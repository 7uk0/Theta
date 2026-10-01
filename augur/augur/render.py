"""Render archive payloads as Markdown.

The read commands return deeply nested dicts. Handed to a model as raw JSON
they cost roughly twice the tokens and read worse, so everything gets a
Markdown pass by default and `--json` stays available for machine consumers.

The renderer is deliberately generic — headings from top-level sections,
tables where a list of uniform records appears, bullets elsewhere — so an
extractor change adds fields to the output instead of breaking it.
"""

from __future__ import annotations

from typing import Any

# A list of dicts is only worth a table if the rows are consistent and narrow.
TABLE_MIN_ROWS = 2
TABLE_MAX_COLUMNS = 7
TABLE_MAX_ROWS = 40
MAX_DEPTH = 6


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _fmt_scalar(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        # Game numbers are noisy at full precision.
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    text = str(value).strip()
    return text if text else "—"


def _label(key: Any) -> str:
    return str(key).replace("_", " ").strip().capitalize()


def _cell(value: Any) -> str:
    if _is_scalar(value):
        return _fmt_scalar(value).replace("|", "\\|").replace("\n", " ")
    if isinstance(value, list):
        return ", ".join(_cell(v) for v in value[:4]) + ("…" if len(value) > 4 else "")
    if isinstance(value, dict):
        return ", ".join(f"{k}={_cell(v)}" for k, v in list(value.items())[:3])
    return str(value)


def _tabulate(rows: list[dict[str, Any]]) -> list[str] | None:
    """Render a list of uniform records as a table, or None if it doesn't fit."""
    if len(rows) < TABLE_MIN_ROWS or len(rows) > TABLE_MAX_ROWS:
        return None
    if not all(isinstance(r, dict) and r for r in rows):
        return None

    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(str(key))
    if len(columns) > TABLE_MAX_COLUMNS:
        return None
    # Nested values flatten badly in a cell; keep those as bullets instead.
    for row in rows:
        for value in row.values():
            if isinstance(value, dict) and any(not _is_scalar(v) for v in value.values()):
                return None
            if isinstance(value, list) and any(not _is_scalar(v) for v in value):
                return None

    out = [
        "| " + " | ".join(_label(c) for c in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        out.append("| " + " | ".join(_cell(row.get(c)) for c in columns) + " |")
    return out


def _render_value(value: Any, depth: int, indent: int = 0) -> list[str]:
    pad = "  " * indent
    out: list[str] = []

    if _is_scalar(value):
        return [f"{pad}{_fmt_scalar(value)}"]

    if depth > MAX_DEPTH:
        return [f"{pad}_(nested detail omitted; use --json for the full payload)_"]

    if isinstance(value, list):
        if not value:
            return [f"{pad}_(none)_"]
        if all(_is_scalar(v) for v in value):
            return [f"{pad}- {_fmt_scalar(v)}" for v in value]
        if all(isinstance(v, dict) for v in value):
            table = _tabulate(value)
            if table:
                return [f"{pad}{line}" for line in table]
        for item in value:
            if _is_scalar(item):
                out.append(f"{pad}- {_fmt_scalar(item)}")
                continue
            nested = _render_value(item, depth + 1, indent + 1)
            if not nested:
                continue
            # Hang the record's first field off the bullet rather than emitting a
            # bare dash with an orphaned block under it.
            first = nested[0].lstrip()
            out.append(f"{pad}- {first[2:] if first.startswith('- ') else first}")
            out.extend(nested[1:])
        return out

    if isinstance(value, dict):
        if not value:
            return [f"{pad}_(none)_"]
        scalars = {k: v for k, v in value.items() if _is_scalar(v)}
        nested = {k: v for k, v in value.items() if not _is_scalar(v)}
        for key, item in scalars.items():
            out.append(f"{pad}- **{_label(key)}:** {_fmt_scalar(item)}")
        for key, item in nested.items():
            out.append(f"{pad}- **{_label(key)}:**")
            out.extend(_render_value(item, depth + 1, indent + 1))
        return out

    return [f"{pad}{value}"]


def render_payload(payload: dict[str, Any], *, title: str) -> str:
    """Render any archive payload: scalars as a summary, sections as headings."""
    if not isinstance(payload, dict):
        return f"# {title}\n\n{payload}\n"

    lines = [f"# {title}", ""]

    scalars = {k: v for k, v in payload.items() if _is_scalar(v)}
    sections = {k: v for k, v in payload.items() if not _is_scalar(v)}

    if scalars:
        for key, value in scalars.items():
            lines.append(f"- **{_label(key)}:** {_fmt_scalar(value)}")
        lines.append("")

    for key, value in sections.items():
        lines.append(f"## {_label(key)}")
        lines.append("")
        lines.extend(_render_value(value, depth=1))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_campaign(payload: dict[str, Any]) -> str:
    if not payload.get("save_loaded"):
        message = payload.get("message") or "No campaign has been ingested yet."
        return f"# Campaign status\n\n{message}\n\nRun `augur ingest` to read your newest save.\n"

    empire = payload.get("empire_name") or "Unknown empire"
    date = payload.get("game_date") or "unknown date"
    lines = [f"# {empire} — {date}", ""]
    for key in (
        "campaign_ref",
        "first_game_date",
        "snapshot_count",
        "version",
        "is_active",
        "updated_at",
    ):
        if key in payload:
            lines.append(f"- **{_label(key)}:** {_fmt_scalar(payload[key])}")
    freshness = payload.get("freshness")
    if isinstance(freshness, dict):
        lines.append("- **Freshness:** " + ", ".join(f"{k}={_cell(v)}" for k, v in freshness.items()))
    lines.append("")
    return "\n".join(lines)


def render_events(payload: dict[str, Any]) -> str:
    events = payload.get("events") or []
    if not events:
        return "# Recent events\n\nNo events recorded yet. Ingest at least two saves so changes can be diffed.\n"

    lines = ["# Recent events", ""]
    for event in events:
        if not isinstance(event, dict):
            lines.append(f"- {event}")
            continue
        date = event.get("game_date") or event.get("date") or "?"
        kind = str(event.get("event_type") or event.get("type") or "event").replace("_", " ")
        summary = event.get("summary") or event.get("description") or ""
        lines.append(f"- **{date}** · {kind}" + (f" — {summary}" if summary else ""))
        payload_detail = event.get("payload") or event.get("data")
        if isinstance(payload_detail, dict) and payload_detail:
            detail = ", ".join(f"{_label(k)}: {_cell(v)}" for k, v in payload_detail.items())
            lines.append(f"  - {detail}")
    lines.append("")
    return "\n".join(lines)


def render_brief(payload: dict[str, Any]) -> str:
    """Strategy context: campaign header, then briefing sections, then events."""
    campaign = payload.get("campaign") or {}
    empire = campaign.get("empire_name") or "Unknown empire"
    date = campaign.get("game_date") or "unknown date"

    lines = [f"# Strategic briefing — {empire}, {date}", ""]

    question = payload.get("question")
    if question:
        lines += [f"**Question:** {question}", ""]
    focus = payload.get("focus")
    if focus:
        lines += [f"**Focus:** {focus}", ""]
    if campaign.get("campaign_ref"):
        lines += [
            f"**Campaign ref:** `{campaign['campaign_ref']}` · "
            f"snapshots: {_fmt_scalar(campaign.get('snapshot_count'))}",
            "",
        ]

    for key in ("advisor_custom_instructions", "advisor_memory", "briefing_note"):
        value = payload.get(key)
        if value:
            lines += [f"## {_label(key)}", "", str(value), ""]

    briefing = payload.get("briefing")
    if isinstance(briefing, dict) and briefing:
        lines += ["## Briefing", ""]
        for section, body in briefing.items():
            lines += [f"### {_label(section)}", ""]
            lines += _render_value(body, depth=1)
            lines.append("")

    events = payload.get("recent_events")
    if events:
        lines += ["## Recent events", ""]
        lines += render_events({"events": events}).split("\n")[2:]

    guidance = payload.get("response_guidance")
    if isinstance(guidance, dict) and guidance:
        lines += ["## Response guidance", ""]
        lines += _render_value(guidance, depth=1)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_chronicle(payload: dict[str, Any]) -> str:
    if payload.get("available") is False or payload.get("message") and not payload.get("chapters"):
        return f"# Chronicle\n\n{payload.get('message') or 'No Chronicle available.'}\n"

    lines = ["# Chronicle", ""]
    for key in ("campaign_ref", "chronicle_revision", "chapter_count", "updated_at"):
        if key in payload:
            lines.append(f"- **{_label(key)}:** `{_fmt_scalar(payload[key])}`")
    lines.append("")

    for chapter in payload.get("chapters") or []:
        if not isinstance(chapter, dict):
            continue
        number = chapter.get("chapter_number") or chapter.get("number") or "?"
        title = chapter.get("title") or "Untitled"
        lines += [f"## Chapter {number}: {title}", ""]
        span = " – ".join(
            str(chapter[k]) for k in ("start_date", "end_date") if chapter.get(k)
        )
        if span:
            lines += [f"_{span}_", ""]
        if chapter.get("epigraph"):
            lines += [f"> {chapter['epigraph']}", ""]
        if chapter.get("summary"):
            lines += [str(chapter["summary"]), ""]
        narrative = chapter.get("narrative") or chapter.get("text")
        if narrative:
            lines += [str(narrative), ""]
        for section in chapter.get("sections") or []:
            if isinstance(section, dict):
                lines += [f"### {section.get('heading') or section.get('title') or ''}", ""]
                lines += [str(section.get("body") or section.get("text") or ""), ""]

    current = payload.get("current_era")
    if isinstance(current, dict) and current:
        lines += ["## Current era (unwritten)", ""]
        lines += _render_value(current, depth=1)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
