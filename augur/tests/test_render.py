"""Tests for the Markdown renderers.

The renderers exist to halve the token cost of handing a briefing to a model,
so what matters is that nothing is silently dropped and that nested game data
stays legible.
"""

from __future__ import annotations

from augur.render import (
    render_brief,
    render_campaign,
    render_chronicle,
    render_events,
    render_payload,
)


def test_campaign_header_carries_empire_and_date() -> None:
    out = render_campaign(
        {
            "save_loaded": True,
            "empire_name": "Kilik Cooperative",
            "game_date": "2235.04.01",
            "campaign_ref": "campaign_abc",
            "snapshot_count": 4,
        }
    )

    assert out.startswith("# Kilik Cooperative — 2235.04.01")
    assert "campaign_abc" in out
    assert "Snapshot count:** 4" in out


def test_campaign_without_save_points_at_ingest() -> None:
    out = render_campaign({"save_loaded": False})

    assert "augur ingest" in out


def test_uniform_record_lists_become_tables() -> None:
    out = render_payload(
        {
            "planets": [
                {"id": 10, "name": "Augur Prime", "size": 18},
                {"id": 11, "name": "Second Light", "size": 14},
            ]
        },
        title="Territory",
    )

    assert "| Id | Name | Size |" in out
    assert "| 10 | Augur Prime | 18 |" in out


def test_ragged_records_fall_back_to_bullets_without_bare_dashes() -> None:
    out = render_payload(
        {
            "fleets": [
                {"name": "1st Fleet", "composition": {"ships": [{"class": "corvette"}]}},
                {"name": "2nd Fleet"},
            ]
        },
        title="Military",
    )

    # A nested record is not table-able; it must still hang off its own bullet.
    assert "- **Name:** 1st Fleet" in out
    for line in out.split("\n"):
        assert line.strip() != "-", "orphaned bullet in rendered output"


def test_scalars_are_formatted_for_reading() -> None:
    out = render_payload(
        {"military_power": 4250.5, "at_war": False, "ruler": None, "colonies": 12},
        title="Summary",
    )

    assert "4,250.5" in out
    assert "**At war:** no" in out
    assert "**Ruler:** —" in out


def test_events_render_as_dated_bullets() -> None:
    out = render_events(
        {
            "events": [
                {
                    "game_date": "2233.02.01",
                    "event_type": "war_started",
                    "summary": "The Entente War began.",
                }
            ]
        }
    )

    assert "**2233.02.01**" in out
    assert "war started" in out
    assert "The Entente War began." in out


def test_empty_events_explain_why() -> None:
    out = render_events({"events": []})

    # Event detection is a diff; one snapshot cannot produce events.
    assert "two saves" in out


def test_brief_keeps_question_focus_and_guidance() -> None:
    out = render_brief(
        {
            "campaign": {"empire_name": "Kilik", "game_date": "2235.04.01"},
            "question": "Can I win this war?",
            "focus": "military",
            "briefing": {"military": {"military_power": 4200}},
            "recent_events": [{"game_date": "2234.01.01", "event_type": "war_started"}],
            "response_guidance": {"tone": "direct"},
        }
    )

    assert "Can I win this war?" in out
    assert "**Focus:** military" in out
    assert "### Military" in out
    assert "## Recent events" in out
    assert "## Response guidance" in out


def test_chronicle_renders_chapters_in_order() -> None:
    out = render_chronicle(
        {
            "campaign_ref": "campaign_abc",
            "chronicle_revision": "rev-1",
            "chapters": [
                {
                    "chapter_number": 1,
                    "title": "First Light",
                    "start_date": "2200.01.01",
                    "end_date": "2235.04.01",
                    "epigraph": "We counted the stars before we named them.",
                    "narrative": "The cooperative spread outward.",
                }
            ],
        }
    )

    assert "## Chapter 1: First Light" in out
    assert "> We counted the stars before we named them." in out
    assert "The cooperative spread outward." in out


def test_deep_nesting_is_bounded_not_dropped() -> None:
    deep: dict = {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": "bottom"}}}}}}}}
    out = render_payload(deep, title="Deep")

    assert "--json" in out  # points at the escape hatch rather than truncating silently
