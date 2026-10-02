"""Tests for the dashboard's data layer.

Curses rendering is exercised separately by driving the real binary in a pty
(see `scripts/smoke_dash.py`). Everything here is the pure part: shape
tolerance, net extraction, sparklines, trends and alerts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from augur.context import CampaignContext
from augur.dash import (
    DashState,
    Trend,
    arrow,
    build_state,
    build_trends,
    collect_alerts,
    extract_nets,
    fmt_number,
    fmt_signed,
    sparkline,
    unwrap_sections,
)
from tests.test_context import _make_test_db

# ------------------------------------------------------------------ formatting


def test_numbers_are_grouped_and_rounded() -> None:
    assert fmt_number(4250) == "4,250"
    assert fmt_number(4250.0) == "4,250"
    assert fmt_number(4250.55) == "4,250.6"
    assert fmt_number(None) == "—"
    assert fmt_number(True) == "yes"


def test_signed_values_carry_their_sign() -> None:
    assert fmt_signed(12) == "+12"
    assert fmt_signed(-12) == "-12"
    assert fmt_signed(0) == "+0"
    assert fmt_signed(None) == ""


def test_sparkline_shapes_a_series() -> None:
    line = sparkline([1.0, 5.0, 10.0])

    assert len(line) == 3
    # Ascending input must produce ascending glyphs.
    assert line[0] != line[-1]
    assert all(ch in "▁▂▃▄▅▆▇█" for ch in line)


def test_flat_series_renders_flat_not_noisy() -> None:
    line = sparkline([7.0, 7.0, 7.0, 7.0])

    assert len(set(line)) == 1, "a flat series must not look like variation"


def test_sparkline_needs_two_points() -> None:
    assert sparkline([]) == ""
    assert sparkline([5.0]) == ""


def test_sparkline_falls_back_to_ascii() -> None:
    line = sparkline([1.0, 9.0], unicode_ok=False)

    assert line
    assert all(ch in "_.-~=+*#" for ch in line)


def test_arrow_follows_direction() -> None:
    assert arrow(Trend("x", [1.0, 2.0])) == "↑"
    assert arrow(Trend("x", [2.0, 1.0])) == "↓"
    assert arrow(Trend("x", [2.0, 2.0])) == "→"
    assert arrow(Trend("x", [1.0, 2.0]), unicode_ok=False) == "^"


def test_trend_delta_and_pct() -> None:
    trend = Trend("Military power", [4250.0, 6900.0])

    assert trend.delta == 2650.0
    assert trend.pct == pytest.approx(0.6235, rel=1e-3)
    # A zero baseline cannot yield a percentage.
    assert Trend("x", [0.0, 5.0]).pct is None


# --------------------------------------------------------------- shape handling


def test_nested_and_flat_briefings_both_unwrap() -> None:
    """get_empire_briefing nests under `sections`; get_strategy_context does not."""
    nested = {"campaign": {}, "detail": "compact", "sections": {"economy": {"a": 1}}}
    flat = {"economy": {"a": 1}}

    assert unwrap_sections(nested) == {"economy": {"a": 1}}
    assert unwrap_sections(flat) == flat
    assert unwrap_sections({}) == {}
    assert unwrap_sections(None) == {}  # type: ignore[arg-type]


def test_nets_read_from_net_monthly() -> None:
    nets = extract_nets({"economy": {"net_monthly": {"energy": -12, "alloys": 18}}})

    assert nets == {"energy": -12.0, "alloys": 18.0}


def test_nets_read_from_resources_summary_with_net_suffix() -> None:
    """The real briefing publishes these as `<name>_net` under resources.summary."""
    nets = extract_nets(
        {"economy": {"resources": {"summary": {"energy_net": -12, "alloys_net": 18}}}}
    )

    assert nets == {"energy": -12.0, "alloys": 18.0}


def test_nets_read_from_situation_economy() -> None:
    nets = extract_nets({"situation": {"economy": {"energy_net": -5, "_note": "ignore me"}}})

    assert nets == {"energy": -5.0}
    assert "_note" not in nets


def test_net_monthly_wins_over_summary() -> None:
    nets = extract_nets(
        {
            "economy": {
                "net_monthly": {"energy": 100},
                "resources": {"summary": {"energy_net": -999}},
            }
        }
    )

    assert nets["energy"] == 100.0


# -------------------------------------------------------------------- alerts


def test_war_and_crisis_raise_alerts() -> None:
    alerts = collect_alerts({"situation": {"at_war": True, "war_count": 2}}, [])

    assert any("AT WAR" in a for a in alerts)
    assert any("2" in a for a in alerts)


def test_deficits_are_named() -> None:
    alerts = collect_alerts(
        {"economy": {"net_monthly": {"energy": -12, "alloys": 18, "food": -3}}}, []
    )

    deficit = next(a for a in alerts if a.startswith("DEFICIT"))
    assert "Energy" in deficit
    assert "Food" in deficit
    assert "Alloys" not in deficit, "a positive net is not a deficit"


def test_a_healthy_empire_raises_nothing() -> None:
    assert collect_alerts({"situation": {"at_war": False}, "economy": {}}, []) == []


def test_military_collapse_is_flagged() -> None:
    alerts = collect_alerts({}, [Trend("Military power", [10_000.0, 4_000.0])])

    assert any("MILITARY DOWN" in a for a in alerts)


def test_military_growth_is_not_flagged() -> None:
    alerts = collect_alerts({}, [Trend("Military power", [4_000.0, 10_000.0])])

    assert not any("MILITARY DOWN" in a for a in alerts)


def test_alerts_tolerate_a_nested_briefing() -> None:
    alerts = collect_alerts({"sections": {"situation": {"at_war": True}}}, [])

    assert any("AT WAR" in a for a in alerts)


# --------------------------------------------------------------- against a db


@pytest.fixture()
def archive(tmp_path: Path):
    db, session_id, _ = _make_test_db(tmp_path)
    return db, session_id


def test_trends_come_back_oldest_first(archive) -> None:
    db, session_id = archive

    trends = build_trends(db=db, session_id=session_id)
    military = next(t for t in trends if t.label == "Military power")

    # The fixture inserts 3001 then 3002; a rising series must read as rising.
    assert military.values == sorted(military.values)
    assert military.delta is not None and military.delta > 0


def test_trends_are_empty_without_a_session(archive) -> None:
    db, _ = archive

    assert build_trends(db=db, session_id=None) == []


def test_build_state_populates_every_pane(archive) -> None:
    db, _ = archive
    context = CampaignContext(db=db)
    try:
        state = build_state(context=context, db=db)
    finally:
        context.close()

    assert state.loaded is True
    assert state.empire == "Kilik Cooperative"
    assert state.game_date == "2235.04.01"
    assert state.snapshot_count == 2
    assert state.facts, "empire pane empty"
    assert state.stockpiles, "resources pane empty"
    assert state.trends, "trends pane empty"
    assert state.events, "events pane empty"
    # The fixture empire is at war and running an energy deficit.
    assert any("AT WAR" in a for a in state.alerts)
    assert any("DEFICIT" in a for a in state.alerts)


def test_facts_carry_real_values_not_placeholders(archive) -> None:
    """Regression: the pane once rendered labels with em-dashes because the
    briefing nests its data under `sections`."""
    db, _ = archive
    context = CampaignContext(db=db)
    try:
        state = build_state(context=context, db=db)
    finally:
        context.close()

    facts = dict(state.facts)
    assert facts["Fleet power"] == "4,200"
    assert facts["Colonies"] == "6"
    assert facts["At war"] == "yes"


def test_empty_archive_yields_a_pointer_not_a_crash(tmp_path: Path) -> None:
    from augur.core.database import GameDatabase

    db = GameDatabase(db_path=tmp_path / "empty.db")
    context = CampaignContext(db=db)
    try:
        state = build_state(context=context, db=db)
    finally:
        context.close()
        db.close()

    assert state.loaded is False
    assert state.message
    assert "ingest" in state.message.lower()


def test_dash_state_defaults_are_drawable() -> None:
    """The first frame renders before any data arrives; it must not be None-ish."""
    state = DashState()

    assert state.empire == "—"
    assert state.facts == []
    assert state.alerts == []
