"""Save ingestion: turn a Stellaris ``.sav`` into a stored campaign snapshot.

Upstream this work was split between an Electron main process, a FastAPI
backend, a threaded ingestion manager and a cancellable worker subprocess,
because a GUI cannot block while a 200MB save is parsed. A CLI can block, so
this is the same pipeline with the scheduling machinery removed:

    stable save file
      -> Rust parser session (parse once, query many)
      -> SaveExtractor.get_complete_briefing()
      -> snapshot signals (resolved leader/war/diplomacy names)
      -> history enrichment (diffable state for event detection)
      -> record_snapshot_from_briefing() into SQLite

The resulting row is what every read command and every Claude Code query sees.
Nothing here contacts a model.
"""

from __future__ import annotations

import logging
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from augur.core.database import GameDatabase
from augur.core.history import build_history_enrichment, record_snapshot_from_briefing
from augur.core.json_utils import json_dumps
from augur.core.utils import compute_save_hash_from_briefing
from augur.parser.save_loader import find_most_recent_save, get_platform_save_paths

logger = logging.getLogger(__name__)

# Stellaris writes the save zip in place. Parsing mid-write yields a truncated
# archive, so wait for size+mtime to hold still before touching it.
STABLE_WINDOW_SECONDS = 0.6
STABLE_MAX_WAIT_SECONDS = 15.0


class IngestError(RuntimeError):
    """Raised when a save cannot be ingested."""


@dataclass
class IngestResult:
    save_path: Path
    session_id: str
    snapshot_id: int | None
    inserted: bool
    game_date: str | None
    empire_name: str | None
    save_hash: str | None
    duration_ms: float
    timings: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "save_path": str(self.save_path),
            "session_id": self.session_id,
            "snapshot_id": self.snapshot_id,
            # False means this exact save state was already recorded; the stored
            # briefing is still refreshed so extractor fixes land.
            "new_snapshot": self.inserted,
            "game_date": self.game_date,
            "empire_name": self.empire_name,
            "save_hash": self.save_hash,
            "duration_ms": round(self.duration_ms, 1),
            "timings_ms": {k: round(v * 1000, 1) for k, v in self.timings.items()},
            "warnings": self.warnings,
        }


def resolve_save_path(save_path: str | Path | None = None) -> Path:
    """Resolve an explicit save path, or find the newest save on this machine."""
    if save_path:
        candidate = Path(save_path).expanduser()
        if candidate.is_dir():
            from augur.parser.save_loader import find_most_recent_save_in_directory

            found = find_most_recent_save_in_directory(candidate)
            if found is None:
                raise IngestError(f"No .sav files found in {candidate}")
            return found
        if not candidate.exists():
            raise IngestError(f"Save file not found: {candidate}")
        return candidate

    found = find_most_recent_save()
    if found is None:
        searched = "\n  ".join(str(p) for p in get_platform_save_paths())
        raise IngestError(
            "No Stellaris save found in the default locations. Searched:\n  "
            f"{searched}\nPass --save with an explicit path or directory."
        )
    return found


def wait_for_stable_save(
    save_path: Path,
    *,
    window_seconds: float = STABLE_WINDOW_SECONDS,
    max_wait_seconds: float = STABLE_MAX_WAIT_SECONDS,
) -> bool:
    """Block until the save stops changing. Returns False on timeout."""
    deadline = time.time() + max_wait_seconds
    last_signature: tuple[int, float] | None = None
    stable_since: float | None = None

    while time.time() < deadline:
        try:
            stat = save_path.stat()
        except OSError:
            time.sleep(0.1)
            continue

        signature = (stat.st_size, stat.st_mtime)
        if signature == last_signature:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= window_seconds and _is_readable_zip(save_path):
                return True
        else:
            last_signature = signature
            stable_since = None
        time.sleep(0.1)

    return _is_readable_zip(save_path)


def _is_readable_zip(save_path: Path) -> bool:
    """A Stellaris save is a zip of gamestate + meta. Confirm both are present."""
    try:
        with zipfile.ZipFile(save_path) as archive:
            names = set(archive.namelist())
        return "gamestate" in names and "meta" in names
    except (OSError, zipfile.BadZipFile):
        return False


def ingest_save(
    *,
    db: GameDatabase,
    save_path: str | Path | None = None,
    wait_for_stable: bool = True,
) -> IngestResult:
    """Parse a save and record a snapshot. The one write path into the archive."""
    started = time.time()
    timings: dict[str, float] = {}
    warnings: list[str] = []

    resolved = resolve_save_path(save_path)

    if wait_for_stable:
        mark = time.time()
        if not wait_for_stable_save(resolved):
            raise IngestError(
                f"{resolved.name} is still being written (or is not a valid save archive). "
                "Retry once the game finishes saving."
            )
        timings["wait_for_stable"] = time.time() - mark

    # Imported here so `augur --help` and the read-only commands never pay for
    # loading the extractor or spawning the parser process.
    from augur.extractor import SaveExtractor
    from augur.parser.rust_bridge import ParserError
    from augur.parser.rust_bridge import session as rust_session

    try:
        mark = time.time()
        with rust_session(str(resolved)):
            timings["parse"] = time.time() - mark

            extractor = SaveExtractor(str(resolved))

            mark = time.time()
            briefing = extractor.get_complete_briefing()
            timings["briefing"] = time.time() - mark

            if not isinstance(briefing, dict) or not briefing:
                raise IngestError(f"Extractor produced no briefing for {resolved.name}")

            # History enrichment feeds between-save event detection. Best effort:
            # a snapshot without it is still worth recording.
            mark = time.time()
            try:
                from augur.core.signals import build_snapshot_signals

                signals = build_snapshot_signals(extractor=extractor, briefing=briefing)
                player_id = (briefing.get("meta") or {}).get("player_id")
                history = build_history_enrichment(
                    gamestate=None,
                    player_id=player_id,
                    precomputed_signals=signals,
                )
                if history:
                    briefing = dict(briefing)
                    briefing["history"] = history
            except Exception as exc:  # noqa: BLE001 - enrichment is optional
                warnings.append(f"history enrichment skipped: {exc}")
                logger.warning("History enrichment failed: %s", exc)
            timings["history"] = time.time() - mark

            missing_dlcs = _safe_call(extractor, "get_missing_dlcs")
            if isinstance(missing_dlcs, list) and missing_dlcs:
                meta = dict(briefing.get("meta") or {})
                meta["missing_dlcs"] = missing_dlcs
                briefing = dict(briefing)
                briefing["meta"] = meta
    except ParserError as exc:
        raise IngestError(f"Rust parser failed on {resolved.name}: {exc}") from exc
    except FileNotFoundError as exc:
        # Almost always the unbuilt parser binary.
        raise IngestError(
            f"{exc}\nBuild it with: cd stellaris-parser && cargo build --release"
        ) from exc

    mark = time.time()
    briefing_json = json_dumps(briefing, default=str)
    timings["serialize"] = time.time() - mark

    save_hash = compute_save_hash_from_briefing(briefing)

    mark = time.time()
    inserted, snapshot_id, session_id = record_snapshot_from_briefing(
        db=db,
        save_path=resolved,
        save_hash=save_hash,
        briefing=briefing,
        briefing_json=briefing_json,
    )
    timings["persist"] = time.time() - mark

    meta = briefing.get("meta") or {}
    return IngestResult(
        save_path=resolved,
        session_id=session_id,
        snapshot_id=snapshot_id,
        inserted=inserted,
        game_date=meta.get("date"),
        empire_name=meta.get("empire_name") or meta.get("name"),
        save_hash=save_hash,
        duration_ms=(time.time() - started) * 1000,
        timings=timings,
        warnings=warnings,
    )


def _safe_call(obj: Any, method: str) -> Any:
    fn = getattr(obj, method, None)
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception:  # noqa: BLE001 - optional metadata
        return None
