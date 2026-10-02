"""Tests for save resolution and the save-stability guard.

The parse itself needs the Rust binary and a real save, so those tests are
marked `integration` and skip when either is missing. Everything around the
parse — path resolution, zip validation, the partial-write guard — runs always,
because that is where a save gets eaten mid-write.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from augur.ingest import IngestError, _is_readable_zip, resolve_save_path, wait_for_stable_save


def _write_save(path: Path, *, valid: bool = True) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("gamestate", 'date="2240.03.01"\n')
        if valid:
            archive.writestr("meta", 'version="Phoenix v4.5.1"\n')
    return path


def test_explicit_save_file_is_used(tmp_path: Path) -> None:
    save = _write_save(tmp_path / "chosen.sav")

    assert resolve_save_path(save) == save


def test_directory_resolves_to_newest_save(tmp_path: Path) -> None:
    older = _write_save(tmp_path / "older.sav")
    newer = _write_save(tmp_path / "newer.sav")
    import os

    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    assert resolve_save_path(tmp_path) == newer


def test_missing_path_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="not found"):
        resolve_save_path(tmp_path / "absent.sav")


def test_empty_directory_is_a_clear_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(IngestError, match="No .sav files"):
        resolve_save_path(empty)


def test_a_save_needs_both_gamestate_and_meta(tmp_path: Path) -> None:
    assert _is_readable_zip(_write_save(tmp_path / "good.sav")) is True
    assert _is_readable_zip(_write_save(tmp_path / "partial.sav", valid=False)) is False


def test_truncated_archive_is_rejected(tmp_path: Path) -> None:
    """A save caught mid-write is not a valid zip; parsing it would throw."""
    half_written = tmp_path / "truncated.sav"
    good = _write_save(tmp_path / "good.sav")
    half_written.write_bytes(good.read_bytes()[: len(good.read_bytes()) // 2])

    assert _is_readable_zip(half_written) is False


def test_stability_wait_accepts_a_settled_save(tmp_path: Path) -> None:
    save = _write_save(tmp_path / "settled.sav")

    assert wait_for_stable_save(save, window_seconds=0.05, max_wait_seconds=3.0) is True


def test_stability_wait_times_out_on_an_invalid_save(tmp_path: Path) -> None:
    broken = tmp_path / "broken.sav"
    broken.write_bytes(b"not a zip at all")

    assert wait_for_stable_save(broken, window_seconds=0.05, max_wait_seconds=0.5) is False


@pytest.mark.integration
def test_full_pipeline_against_a_real_save(tmp_path: Path) -> None:
    """Parse -> extract -> persist, end to end. Needs AUGUR_TEST_SAVE and the parser."""
    import os

    save = os.environ.get("AUGUR_TEST_SAVE")
    if not save or not Path(save).exists():
        pytest.skip("set AUGUR_TEST_SAVE to a .sav path to run the full pipeline")

    from augur.core.database import GameDatabase
    from augur.ingest import ingest_save
    from augur.parser.rust_bridge import PARSER_BINARY

    if not Path(PARSER_BINARY).exists():
        pytest.skip("Rust parser not built: cd stellaris-parser && cargo build --release")

    db = GameDatabase(db_path=tmp_path / "campaign.db")
    try:
        result = ingest_save(db=db, save_path=save)
        assert result.session_id
        assert result.game_date
        # Re-ingesting the same save must not manufacture a second snapshot.
        again = ingest_save(db=db, save_path=save)
        assert again.inserted is False
    finally:
        db.close()
