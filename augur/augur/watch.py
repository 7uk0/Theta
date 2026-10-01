"""Vigil: keep the archive current while the game is running.

Watches the Stellaris save directory and re-ingests on every autosave, so the
briefing Claude Code reads is never more than one save behind. Optionally
renders a Markdown briefing to disk after each ingest, which makes the whole
system work even for a client that can only read a file.

Run it in a second terminal, or under systemd --user, for the duration of a
play session.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from pathlib import Path

from augur.core.database import GameDatabase
from augur.core.save_watcher import SaveWatcher
from augur.ingest import IngestError, ingest_save

logger = logging.getLogger(__name__)

# Stellaris autosaves monthly on fast speed; one ingest per save is plenty.
COALESCE_SECONDS = 3.0


class Vigil:
    """Serialises save events into one ingest at a time, newest-wins."""

    def __init__(
        self,
        *,
        db: GameDatabase,
        watch_paths: list[Path] | None = None,
        on_ingest=None,
        coalesce_seconds: float = COALESCE_SECONDS,
    ) -> None:
        self._db = db
        self._on_ingest = on_ingest
        self._coalesce_seconds = coalesce_seconds

        self._lock = threading.Lock()
        self._pending: Path | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()

        self._watcher = SaveWatcher(
            watch_paths=watch_paths,
            on_save_detected=self._queue,
            debounce_seconds=1.0,
        )

    def _queue(self, save_path: Path) -> None:
        with self._lock:
            # Newest save wins; an older queued one is already superseded.
            self._pending = save_path
        self._wake.set()

    def run(self, *, ingest_existing: bool = True) -> int:
        if not self._watcher.start():
            searched = "\n  ".join(str(p) for p in self._watcher.watch_paths)
            print(
                "No Stellaris save directory found to watch. Looked in:\n  "
                f"{searched}\nPass --save-dir with the folder holding your .sav files.",
                file=sys.stderr,
            )
            return 1

        for path in self._watcher.get_valid_watch_paths():
            print(f"vigil: watching {path}", file=sys.stderr)

        self._install_signal_handlers()

        if ingest_existing:
            latest = self._watcher.find_latest_save()
            if latest is not None:
                self._queue(latest)

        try:
            while not self._stop.is_set():
                self._wake.wait(timeout=1.0)
                if self._stop.is_set():
                    break
                if not self._wake.is_set():
                    continue

                # Let a burst of write events settle, then take the newest.
                time.sleep(self._coalesce_seconds)
                self._wake.clear()
                with self._lock:
                    target, self._pending = self._pending, None
                if target is None:
                    continue

                self._ingest_once(target)
        finally:
            self._watcher.stop()
            print("vigil: stopped", file=sys.stderr)
        return 0

    def _ingest_once(self, save_path: Path) -> None:
        try:
            result = ingest_save(db=self._db, save_path=save_path)
        except IngestError as exc:
            print(f"vigil: {exc}", file=sys.stderr)
            return
        except Exception as exc:  # noqa: BLE001 - a vigil must survive one bad save
            logger.exception("Unexpected ingest failure")
            print(f"vigil: unexpected ingest failure: {exc}", file=sys.stderr)
            return

        state = "new snapshot" if result.inserted else "refreshed"
        print(
            f"vigil: {result.game_date or '?'} {result.empire_name or '?'} "
            f"({state}, {result.duration_ms / 1000:.1f}s)",
            file=sys.stderr,
        )
        if self._on_ingest is not None:
            try:
                self._on_ingest(result)
            except Exception as exc:  # noqa: BLE001 - rendering must not kill the vigil
                print(f"vigil: post-ingest hook failed: {exc}", file=sys.stderr)

    def _install_signal_handlers(self) -> None:
        def handle(signum, frame):  # noqa: ARG001
            self._stop.set()
            self._wake.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handle)
            except (ValueError, OSError):
                # Not on the main thread; the caller owns shutdown.
                pass
