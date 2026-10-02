"""Tests for the writer-lifecycle bugs found in the bug-hunt pass.

Every case here was a silent failure: the autosave was lost, or the campaign was
split in two, with nothing on screen to say so. They are the tests most worth
having, because the symptom is invisible.
"""

from __future__ import annotations

import threading
import time
import zipfile
from pathlib import Path

import pytest

from augur.core.database import GameDatabase
from augur.core.json_utils import json_dumps
from augur.ingest import _INGEST_LOCK, wait_for_stable_save


def _make_save(path: Path, date: str = "2240.03.01") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("gamestate", f'date="{date}"\n')
        archive.writestr("meta", f'version="Phoenix v4.5.1"\ndate="{date}"\n')
    return path


# ------------------------------------------------------------ stability guard


def test_stability_guard_fails_a_file_that_keeps_growing(tmp_path: Path) -> None:
    """It documented "Returns False on timeout" and returned True instead.

    A save still being written would then be handed to the parser — the exact
    thing the guard exists to prevent.
    """
    target = tmp_path / "growing.sav"
    _make_save(target)
    stop = threading.Event()

    def keep_writing() -> None:
        size = 1
        while not stop.is_set():
            # Stays a valid zip the whole time, so only the settling check can
            # catch it.
            with zipfile.ZipFile(target, "w") as archive:
                archive.writestr("gamestate", 'date="2240.03.01"' + "x" * size)
                archive.writestr("meta", 'version="x"')
            size += 512
            time.sleep(0.05)

    writer = threading.Thread(target=keep_writing, daemon=True)
    writer.start()
    try:
        assert wait_for_stable_save(target, window_seconds=0.2, max_wait_seconds=1.5) is False
    finally:
        stop.set()
        writer.join(timeout=5)


def test_stability_guard_passes_a_settled_save(tmp_path: Path) -> None:
    target = _make_save(tmp_path / "settled.sav")

    assert wait_for_stable_save(target, window_seconds=0.1, max_wait_seconds=3.0) is True


# ------------------------------------------------------------- ingest locking


def test_ingest_is_serialised_process_wide() -> None:
    """Two ingests racing on a fresh archive each created their own session,
    permanently splitting the campaign's history."""
    assert not _INGEST_LOCK.locked()

    order: list[str] = []
    released = threading.Event()

    def hold() -> None:
        with _INGEST_LOCK:
            order.append("first-in")
            released.wait(timeout=5)
            order.append("first-out")

    def follow() -> None:
        with _INGEST_LOCK:
            order.append("second-in")

    a = threading.Thread(target=hold)
    a.start()
    while not order:
        time.sleep(0.01)
    b = threading.Thread(target=follow)
    b.start()
    time.sleep(0.2)

    # The second ingest must still be waiting, not interleaved.
    assert order == ["first-in"]
    released.set()
    a.join(timeout=5)
    b.join(timeout=5)
    assert order == ["first-in", "first-out", "second-in"]


# ---------------------------------------------------- vigil stop and shutdown


def test_vigil_does_not_start_an_ingest_after_stop(tmp_path: Path) -> None:
    """stop() during the coalesce window used to be ignored, so an ingest began
    after shutdown and then met a closed database."""
    from augur.watch import Vigil

    watch_dir = tmp_path / "saves"
    watch_dir.mkdir()
    db = GameDatabase(db_path=tmp_path / "campaign.db")
    ingested: list[Path] = []

    vigil = Vigil(db=db, watch_paths=[watch_dir], coalesce_seconds=2.0, log=lambda _m: None)
    # Record attempts rather than really parsing.
    vigil._ingest_once = lambda path: ingested.append(path)  # type: ignore[method-assign]

    thread = threading.Thread(target=vigil.run, kwargs={"ingest_existing": False}, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 5
        while not vigil.is_running and time.time() < deadline:
            time.sleep(0.05)
        assert vigil.is_running

        vigil._queue(_make_save(watch_dir / "autosave.sav"))
        time.sleep(0.2)  # inside the coalesce window
        vigil.stop()
        thread.join(timeout=5)
    finally:
        db.close()

    assert ingested == [], "an ingest started after stop()"
    assert vigil.is_running is False


def test_vigil_can_drain_the_save_that_shutdown_raced(tmp_path: Path) -> None:
    """The queued autosave must not be dropped just because quit arrived first."""
    from augur.watch import Vigil

    watch_dir = tmp_path / "saves"
    watch_dir.mkdir()
    db = GameDatabase(db_path=tmp_path / "campaign.db")
    ingested: list[Path] = []

    vigil = Vigil(db=db, watch_paths=[watch_dir], coalesce_seconds=2.0, log=lambda _m: None)
    vigil._ingest_once = lambda path: ingested.append(path)  # type: ignore[method-assign]

    thread = threading.Thread(target=vigil.run, kwargs={"ingest_existing": False}, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 5
        while not vigil.is_running and time.time() < deadline:
            time.sleep(0.05)

        save = _make_save(watch_dir / "autosave.sav")
        vigil._queue(save)
        time.sleep(0.2)
        vigil.stop()
        thread.join(timeout=5)

        assert ingested == []
        # Still open here, so the queued save is recoverable.
        assert vigil.drain_pending() is True
        assert ingested == [save]
        assert vigil.drain_pending() is False
    finally:
        db.close()


def test_vigil_reports_not_running_when_it_could_not_start(tmp_path: Path) -> None:
    """The footer claimed "vigil running" even when the thread had already died."""
    from augur.watch import Vigil

    db = GameDatabase(db_path=tmp_path / "campaign.db")
    try:
        vigil = Vigil(
            db=db, watch_paths=[tmp_path / "nonexistent"], log=lambda _m: None
        )
        assert vigil.run(ingest_existing=False) == 1
        assert vigil.is_running is False
    finally:
        db.close()


def test_dashboard_shutdown_waits_for_its_writers(tmp_path: Path) -> None:
    """Closing the database under a running ingest raised ProgrammingError deep
    in the write, losing the autosave or leaving a snapshot with no events."""
    from augur.dash import Dashboard

    db = GameDatabase(db_path=tmp_path / "campaign.db")
    finished = threading.Event()
    started = threading.Event()

    def slow_write() -> None:
        started.set()
        time.sleep(0.6)
        # Must still be usable: shutdown should not have closed it yet.
        db.get_or_create_active_session(
            save_id="late", save_path="/t.sav", empire_name="Late", last_game_date="2250.01.01"
        )
        finished.set()

    dash = Dashboard(db=db, watch=False)
    worker = threading.Thread(target=slow_write, daemon=True)
    dash._ingest_threads.append(worker)
    worker.start()
    started.wait(timeout=5)

    dash.shutdown(timeout=10)

    assert finished.is_set(), "shutdown returned while a writer was still going"
    # Only now is closing safe, which is the order run_dashboard relies on.
    db.close()


def test_dashboard_refuses_a_second_concurrent_reread(tmp_path: Path) -> None:
    """Holding 'r' spawned one thread and one full save parse per keypress."""
    from augur.dash import Dashboard

    db = GameDatabase(db_path=tmp_path / "campaign.db")
    try:
        dash = Dashboard(db=db, watch=False)
        blocker = threading.Event()
        busy = threading.Thread(target=lambda: blocker.wait(timeout=5), daemon=True)
        dash._ingest_threads.append(busy)
        busy.start()

        dash._ingest_now()
        assert any("already running" in line for line in dash._log)
        # No second worker was added.
        assert len([t for t in dash._ingest_threads if t.is_alive()]) == 1
        blocker.set()
        busy.join(timeout=5)
    finally:
        db.close()


def test_manual_reread_honours_the_watched_directory(tmp_path: Path) -> None:
    """'r' called ingest_save(None), which searches only the platform defaults —
    so it could ingest a different campaign, or nothing at all."""
    from augur.dash import Dashboard

    watch_dir = tmp_path / "saves"
    watch_dir.mkdir()
    db = GameDatabase(db_path=tmp_path / "campaign.db")
    try:
        dash = Dashboard(db=db, watch=False, watch_paths=[watch_dir])
        assert dash._explicit_save_dir() == watch_dir

        default = Dashboard(db=db, watch=False)
        assert default._explicit_save_dir() is None
    finally:
        db.close()


# ----------------------------------------------------------- WAL change signal


def test_change_detection_survives_wal_mode(tmp_path: Path) -> None:
    """A concurrent writer appends to <db>-wal and leaves the main file's mtime
    untouched, so watching it alone froze `dash --no-watch` on one frame."""
    from augur.dash import Dashboard

    path = tmp_path / "campaign.db"
    writer = GameDatabase(db_path=path)
    session = writer.get_or_create_active_session(
        save_id="s", save_path="/t.sav", empire_name="E", last_game_date="2200.01.01"
    )
    writer.insert_snapshot(
        session_id=session, game_date="2200.01.01", save_hash="h1", military_power=1,
        colony_count=1, wars_count=0, energy_net=0, alloys_net=0,
        full_briefing_json=json_dumps({}), event_state_json=json_dumps({}),
    )

    reader = GameDatabase(db_path=path)
    try:
        dash = Dashboard(db=reader, watch=False)
        before = dash._db_fingerprint()
        main_mtime_before = path.stat().st_mtime_ns

        writer.insert_snapshot(
            session_id=session, game_date="2210.01.01", save_hash="h2", military_power=2,
            colony_count=2, wars_count=0, energy_net=0, alloys_net=0,
            full_briefing_json=json_dumps({}), event_state_json=json_dumps({}),
        )

        after = dash._db_fingerprint()
        assert after != before, "the write was invisible to the change detector"
        # The point of the fix: the main file alone would have missed it.
        if path.with_name(path.name + "-wal").exists():
            assert path.stat().st_mtime_ns == main_mtime_before
    finally:
        reader.close()
        writer.close()


# --------------------------------------------------------- session consistency


def test_trends_scope_to_the_same_session_as_the_header(tmp_path: Path) -> None:
    """build_trends used db.get_sessions(limit=1), which orders differently and
    does not filter trashed playthroughs — so the trends pane could show an
    abandoned campaign under the active campaign's header."""
    from augur.context import CampaignContext
    from augur.dash import _current_session_id

    db = GameDatabase(db_path=tmp_path / "campaign.db")
    try:
        old = db.get_or_create_active_session(
            save_id="old", save_path="/old.sav", empire_name="Abandoned",
            last_game_date="2300.01.01",
        )
        db.end_session(session_id=old)
        current = db.get_or_create_active_session(
            save_id="new", save_path="/new.sav", empire_name="Active",
            last_game_date="2205.01.01",
        )

        context = CampaignContext(db=db)
        try:
            assert context.current_session_id() == current
            assert _current_session_id(context) == current
            # The header and the trends must agree on which campaign this is.
            assert context.get_active_campaign()["empire_name"] == "Active"
        finally:
            context.close()
    finally:
        db.close()


# -------------------------------------------------------------- net extraction


def test_a_held_stockpile_is_not_reported_as_monthly_income() -> None:
    """resources.summary mixes `minor_artifacts` (a total held) in among the
    `<name>_net` fields, so the ledger showed `Minor Artifacts 312 +312/mo`."""
    from augur.dash import extract_nets

    nets = extract_nets(
        {
            "economy": {
                "resources": {
                    "summary": {
                        "energy_net": -12,
                        "minor_artifacts": 312,
                    }
                }
            }
        }
    )

    assert nets == {"energy": -12.0}
    assert "minor_artifacts" not in nets


def test_deficit_count_is_not_treated_as_a_resource() -> None:
    from augur.dash import extract_nets

    nets = extract_nets({"situation": {"economy": {"energy_net": -5, "resources_in_deficit": 3}}})

    assert nets == {"energy": -5.0}


@pytest.mark.parametrize("key", ["minor_artifacts", "resources_in_deficit"])
def test_non_net_keys_never_reach_the_alert_logic(key: str) -> None:
    """A stockpile misread as a net would raise a phantom DEFICIT alert."""
    from augur.dash import collect_alerts

    alerts = collect_alerts({"economy": {"resources": {"summary": {key: -1}}}}, [])

    assert not any("DEFICIT" in a for a in alerts)
