#!/usr/bin/env python3
"""Drive the curses dashboard in a real pty and assert the panes rendered.

Unit tests cover the dashboard's data layer; this covers the part they cannot —
that curses actually paints, that no write runs off the screen, that 'q' exits
cleanly, and that the glyph fallback behaves. It runs the installed command in a
child pty, scrapes the byte stream, and checks for expected content.

    python3 scripts/smoke_dash.py                      # uses a temp archive
    python3 scripts/smoke_dash.py --db path/to.db      # uses a real one
    python3 scripts/smoke_dash.py --rows 12 --cols 50  # short terminal
    python3 scripts/smoke_dash.py --rows 8 --cols 40    # below the minimum

Exit status is 0 when every required marker was found.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import pty
import select
import struct
import sys
import termios
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Markers that must appear for the frame to count as drawn. SITUATION LOG is the
# invariant that matters: the pane the dashboard exists for must never be
# squeezed out by the panes above it, at any size that renders at all.
REQUIRED = ["EMPIRE", "RESOURCE LEDGER", "SITUATION LOG", "[q] quit", "[r] re-read"]
# The trajectory pane is allowed to yield its rows to the log on a short
# terminal, so only demand it when there is room for both.
WITH_DATA = ["TRAJECTORY"]
ROOMY_ROWS = 22


def build_demo_archive(path: Path) -> None:
    """A two-snapshot campaign, so trends and events both have something to show."""
    from augur.core.database import GameDatabase
    from augur.core.json_utils import json_dumps

    db = GameDatabase(db_path=path)
    session_id = db.get_or_create_active_session(
        save_id="smoke-save",
        save_path="/tmp/smoke.sav",
        empire_name="Smoke Test Collective",
        last_game_date="2250.06.01",
    )
    briefing = {
        "meta": {"date": "2250.06.01", "version": "Phoenix v4.5.1", "campaign_id": "smoke"},
        "identity": {
            "name": "Smoke Test Collective",
            "empire_name": "Smoke Test Collective",
            "authority": "auth_imperial",
            "ethics": ["ethic_fanatic_militarist", "ethic_authoritarian"],
        },
        "situation": {"game_phase": "mid", "at_war": True, "war_count": 1, "contact_count": 7},
        "economy": {
            "resources": {
                "stockpiles": {"energy": 5400, "minerals": 3100, "alloys": 980},
                "summary": {"energy_net": -42, "minerals_net": 110, "alloys_net": 25},
            }
        },
        "military": {"military_power": 18200, "military_fleets": 6},
        "territory": {"colonies": {"total_count": 11, "total_population": 240}},
        "technology": {"tech_count": 134},
    }
    db.update_session_latest_briefing(
        session_id=session_id,
        latest_briefing_json=json_dumps(briefing),
        last_game_date="2250.06.01",
    )
    ids = []
    for index, (date, power) in enumerate(
        (("2244.01.01", 9000), ("2247.03.01", 14000), ("2250.06.01", 18200)), start=1
    ):
        ids.append(
            db.insert_snapshot(
                session_id=session_id,
                game_date=date,
                save_hash=f"smoke-{index}",
                military_power=power,
                colony_count=8 + index,
                wars_count=1,
                energy_net=-42,
                alloys_net=25,
                full_briefing_json=json_dumps(briefing),
                event_state_json=json_dumps(briefing),
            )
        )
    db.insert_events(
        session_id=session_id,
        captured_at=int(time.time()),
        game_date="2250.06.01",
        events=[
            {
                "event_type": "war_started",
                "summary": "The Collective entered the Rimward War.",
                "data": {"from_snapshot_id": ids[0], "to_snapshot_id": ids[-1]},
            }
        ],
    )
    db.close()


def run_in_pty(db_path: Path, rows: int, cols: int, settle: float) -> tuple[int, str]:
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.update(
            TERM="xterm-256color",
            AUGUR_DB=str(db_path),
            COLUMNS=str(cols),
            LINES=str(rows),
            PYTHONPATH=str(REPO_ROOT),
        )
        os.execv(sys.executable, [sys.executable, "-m", "augur.cli", "dash", "--no-watch"])

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    chunks: list[bytes] = []

    def drain(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                continue
            try:
                data = os.read(fd, 65536)
            except OSError:
                return
            if not data:
                return
            chunks.append(data)

    drain(settle)
    try:
        os.write(fd, b"q")
    except OSError:
        pass
    drain(1.5)

    _, status = os.waitpid(pid, 0)
    os.close(fd)
    return os.waitstatus_to_exitcode(status), b"".join(chunks).decode("utf-8", "replace")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="Existing archive to display.")
    parser.add_argument("--rows", type=int, default=34)
    parser.add_argument("--cols", type=int, default=110)
    parser.add_argument("--settle", type=float, default=4.0, help="Seconds to let it draw.")
    args = parser.parse_args()

    if args.db:
        db_path = Path(args.db).expanduser()
        if not db_path.exists():
            print(f"no archive at {db_path}", file=sys.stderr)
            return 1
        temp = None
    else:
        import tempfile

        temp = tempfile.TemporaryDirectory()
        db_path = Path(temp.name) / "smoke.db"
        build_demo_archive(db_path)

    try:
        code, text = run_in_pty(db_path, args.rows, args.cols, args.settle)
    finally:
        if temp is not None:
            temp.cleanup()

    print(f"exit code: {code}   bytes captured: {len(text)}")

    failures: list[str] = []
    if code != 0:
        failures.append(f"non-zero exit: {code}")
    if "Traceback" in text:
        failures.append("traceback in output")

    cramped = args.rows < 11 or args.cols < 72
    # An archive with no campaign draws the pointer-to-ingest frame instead of
    # the panes. That is correct, so assert that frame rather than the panes.
    empty = "No campaign" in text or "ingested yet" in text

    if cramped:
        expected = ["augur needs"]
    elif empty:
        expected = ["augur ·", "ingest", "Press r"]
        print("  (archive holds no campaign — checking the empty frame)")
    else:
        expected = list(REQUIRED)
        if args.rows >= ROOMY_ROWS:
            expected += WITH_DATA

    for marker in expected:
        found = marker in text
        print(f"  [{'ok' if found else 'XX'}] {marker}")
        if not found:
            failures.append(f"missing: {marker}")

    glyphs = [g for g in "▁▂▃▄▅▆▇█" if g in text]
    arrows = [g for g in "↑↓→" if g in text]
    if not cramped and not empty and args.rows >= ROOMY_ROWS:
        print(f"  [{'ok' if glyphs else 'XX'}] sparkline glyphs {glyphs}")
        print(f"  [{'ok' if arrows else 'XX'}] trend arrows {arrows}")
        if not glyphs:
            failures.append("no sparkline rendered")
    elif not cramped and not empty:
        print(f"  (short terminal: trajectory pane may yield to the log — glyphs {glyphs})")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
