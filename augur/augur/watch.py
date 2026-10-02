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
import traceback
from pathlib import Path

from augur.core.database import GameDatabase
from augur.core.save_watcher import SaveWatcher
from augur.ingest import IngestError, ingest_save

logger = logging.getLogger(__name__)


def _log_to_stderr(message: str) -> None:
    print(f"vigil: {message}", file=sys.stderr)

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
        log=None,
    ) -> None:
        self._db = db
        self._on_ingest = on_ingest
        self._coalesce_seconds = coalesce_seconds
        # A curses front end must capture these lines rather than let them be
        # printed over the display, so the sink is injectable.
        self._log = log if log is not None else _log_to_stderr

        self._lock = threading.Lock()
        self._pending: Path | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._running = False

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
            self._log(
                "No Stellaris save directory found to watch. Looked in:\n  "
                f"{searched}\nPass --save-dir with the folder holding your .sav files."
            )
            return 1

        self._running = True
        for path in self._watcher.get_valid_watch_paths():
            self._log(f"watching {path}")

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
                # Waiting on the stop event rather than sleeping means a
                # shutdown during the coalesce window is honoured instead of
                # starting an ingest that will outlive the database.
                if self._stop.wait(timeout=self._coalesce_seconds):
                    break
                self._wake.clear()
                with self._lock:
                    target, self._pending = self._pending, None
                if target is None:
                    continue
                if self._stop.is_set():
                    # Put it back so a drain can still record it.
                    with self._lock:
                        self._pending = target
                    break

                self._ingest_once(target)
        finally:
            self._watcher.stop()
            self._running = False
            self._log("stopped")
        return 0

    def drain_pending(self) -> bool:
        """Ingest the newest queued save, if shutdown raced one.

        `stop()` during a coalesce window leaves the most recent autosave queued
        and unrecorded. The owner calls this after the loop exits — while the
        database is still open — so the save is not lost.
        """
        with self._lock:
            target, self._pending = self._pending, None
        if target is None:
            return False
        self._log(f"draining queued save {target.name}")
        self._ingest_once(target)
        return True

    @property
    def is_running(self) -> bool:
        return self._running

    def _ingest_once(self, save_path: Path) -> None:
        try:
            result = ingest_save(db=self._db, save_path=save_path)
        except IngestError as exc:
            self._log(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a vigil must survive one bad save
            # logger.exception() would write a traceback to stderr, which is the
            # terminal a curses front end is painting. Send it to the sink.
            self._log(f"unexpected ingest failure: {exc}")
            self._log(traceback.format_exc().strip().splitlines()[-1])
            return

        state = "new snapshot" if result.inserted else "refreshed"
        self._log(
            f"{result.game_date or '?'} {result.empire_name or '?'} "
            f"({state}, {result.duration_ms / 1000:.1f}s)"
        )
        if self._on_ingest is not None:
            try:
                self._on_ingest(result)
            except Exception as exc:  # noqa: BLE001 - rendering must not kill the vigil
                self._log(f"post-ingest hook failed: {exc}")

    def stop(self) -> None:
        """Ask the loop to exit. Safe to call from another thread."""
        self._stop.set()
        self._wake.set()

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
