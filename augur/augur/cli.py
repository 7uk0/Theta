"""augur — the command surface.

This replaces the upstream MCP server. Where that project spoke JSON-RPC over
stdio to an MCP client, this speaks plain stdout to whatever runs it, which
means Claude Code reaches the campaign archive through Bash and needs no API
key, no server process and no MCP configuration.

The mapping from the old tool surface:

    get_active_campaign            -> augur status
    get_strategy_context           -> augur brief
    get_empire_briefing            -> augur detail
    get_recent_events              -> augur events
    get_cached_chronicle           -> augur chronicle read
    get_chronicle_source_material  -> augur chronicle source
    save_chronicle_current_era     -> augur chronicle save
    update_chronicle_chapter       -> augur chronicle update
    create_chronicle_chapter       -> augur chronicle create
    undo_chronicle_edit            -> augur chronicle undo

Exit codes are meaningful so a caller can tell states apart:
    0 success · 1 error · 2 usage · 3 nothing ingested yet
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from augur import __version__
from augur.context import CampaignContext, ContextError
from augur.core.database import GameDatabase
from augur.parser.paths import get_repo_root, get_state_dir

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_CAMPAIGN = 3

DEFAULT_DB_NAME = "campaign.db"
FOCUS_CHOICES = [
    "auto",
    "general",
    "economy",
    "military",
    "diplomacy",
    "technology",
    "territory",
    "crisis",
    "chronicle",
]


def resolve_db_path(explicit: str | None = None) -> Path:
    """Database location: --db, then AUGUR_DB, then the XDG state dir."""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("AUGUR_DB")
    if env:
        return Path(env).expanduser()
    return get_state_dir() / DEFAULT_DB_NAME


def _emit(payload: Any, *, as_json: bool, renderer=None, title: str = "augur") -> None:
    if as_json or renderer is None:
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n")
        return
    sys.stdout.write(renderer(payload))


def _read_text_arg(inline: str | None, file_path: str | None, *, label: str) -> str:
    """Prose comes in as --x or --x-file (with '-' meaning stdin).

    Long narrative text through a shell argument is a quoting minefield, so the
    file form is the one documented for callers that generate prose.
    """
    if inline is not None and file_path is not None:
        raise ContextError(f"Pass either --{label} or --{label}-file, not both.")
    if inline is not None:
        return inline
    if file_path is None:
        raise ContextError(f"--{label} or --{label}-file is required.")
    if file_path == "-":
        return sys.stdin.read()
    path = Path(file_path).expanduser()
    if not path.exists():
        raise ContextError(f"File not found: {path}")
    return path.read_text(encoding="utf-8")


def _parse_sections(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [s.strip() for s in raw.split(",") if s.strip()]


# ---------------------------------------------------------------- write side


def cmd_ingest(args: argparse.Namespace) -> int:
    from augur.ingest import IngestError, ingest_save

    db = GameDatabase(db_path=resolve_db_path(args.db))
    try:
        result = ingest_save(db=db, save_path=args.save, wait_for_stable=not args.no_wait)
    except IngestError as exc:
        print(f"augur: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        db.close()

    if args.json:
        _emit(result.to_dict(), as_json=True)
    else:
        state = "recorded new snapshot" if result.inserted else "refreshed existing snapshot"
        print(f"{result.empire_name or 'Unknown empire'} — {result.game_date or 'unknown date'}")
        print(f"  save:    {result.save_path.name}")
        print(f"  archive: {state} (session {result.session_id})")
        print(f"  took:    {result.duration_ms / 1000:.1f}s")
        for warning in result.warnings:
            print(f"  warn:    {warning}", file=sys.stderr)
    return EXIT_OK


def cmd_vigil(args: argparse.Namespace) -> int:
    from augur.watch import Vigil

    db = GameDatabase(db_path=resolve_db_path(args.db))
    render_to = Path(args.render).expanduser() if args.render else None

    def after_ingest(_result) -> None:
        if render_to is None:
            return
        context = CampaignContext(db=db, language=args.language)
        try:
            payload = context.get_strategy_context(question="", focus="general")
        except ContextError:
            return
        from augur.render import render_brief

        render_to.parent.mkdir(parents=True, exist_ok=True)
        render_to.write_text(render_brief(payload), encoding="utf-8")
        print(f"vigil: rendered {render_to}", file=sys.stderr)

    watch_paths = [Path(args.save_dir).expanduser()] if args.save_dir else None
    vigil = Vigil(db=db, watch_paths=watch_paths, on_ingest=after_ingest)
    try:
        return vigil.run(ingest_existing=not args.no_initial)
    finally:
        db.close()


def cmd_saves(args: argparse.Namespace) -> int:
    from augur.parser.save_loader import find_all_saves, get_platform_save_paths

    search = [Path(args.save_dir).expanduser()] if args.save_dir else None
    saves = find_all_saves(search) or []
    saves = saves[: args.limit]

    if args.json:
        _emit([{k: str(v) for k, v in s.items()} for s in saves], as_json=True)
        return EXIT_OK

    if not saves:
        print("No .sav files found. Searched:", file=sys.stderr)
        for path in search or get_platform_save_paths():
            print(f"  {path}", file=sys.stderr)
        return EXIT_ERROR

    for entry in saves:
        name = entry.get("name") or Path(str(entry.get("path", ""))).name
        modified = entry.get("modified") or entry.get("mtime") or ""
        print(f"{modified}  {name}")
        print(f"              {entry.get('path')}")
    return EXIT_OK


# ----------------------------------------------------------------- read side


def _with_context(args: argparse.Namespace):
    db_path = resolve_db_path(args.db)
    if not db_path.exists():
        print(
            f"augur: no campaign archive at {db_path}\n"
            "Run `augur ingest` first to read your newest save.",
            file=sys.stderr,
        )
        return None
    return CampaignContext(db_path=db_path, language=args.language)


def _run_read(args: argparse.Namespace, fn, renderer, title: str) -> int:
    context = _with_context(args)
    if context is None:
        return EXIT_NO_CAMPAIGN
    try:
        payload = fn(context)
    except ContextError as exc:
        print(f"augur: {exc}", file=sys.stderr)
        return EXIT_NO_CAMPAIGN
    finally:
        context.close()
    _emit(payload, as_json=args.json, renderer=renderer, title=title)
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    from augur.render import render_campaign

    return _run_read(args, lambda c: c.get_active_campaign(), render_campaign, "Campaign status")


def cmd_brief(args: argparse.Namespace) -> int:
    from augur.render import render_brief

    return _run_read(
        args,
        lambda c: c.get_strategy_context(
            question=args.question or "",
            focus=args.focus,
            event_limit=args.events,
        ),
        render_brief,
        "Strategic briefing",
    )


def cmd_detail(args: argparse.Namespace) -> int:
    from augur.render import render_payload

    return _run_read(
        args,
        lambda c: c.get_empire_briefing(
            sections=_parse_sections(args.sections),
            max_detail=args.detail,
        ),
        lambda p: render_payload(p, title="Empire briefing"),
        "Empire briefing",
    )


def cmd_events(args: argparse.Namespace) -> int:
    from augur.render import render_events

    return _run_read(
        args,
        lambda c: c.get_recent_events(limit=args.limit, notable_only=args.notable),
        render_events,
        "Recent events",
    )


def cmd_chronicle_read(args: argparse.Namespace) -> int:
    from augur.render import render_chronicle

    return _run_read(args, lambda c: c.get_cached_chronicle(), render_chronicle, "Chronicle")


def cmd_chronicle_source(args: argparse.Namespace) -> int:
    from augur.render import render_payload

    return _run_read(
        args,
        lambda c: c.get_chronicle_source_material(
            scope=args.scope,
            chapter_number=args.chapter,
            max_events=args.max_events,
        ),
        lambda p: render_payload(p, title="Chronicle source material"),
        "Chronicle source material",
    )


def _run_write(args: argparse.Namespace, fn) -> int:
    context = _with_context(args)
    if context is None:
        return EXIT_NO_CAMPAIGN
    try:
        payload = fn(context)
    except ContextError as exc:
        print(f"augur: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        context.close()
    _emit(payload, as_json=True)
    return EXIT_OK


def cmd_chronicle_save(args: argparse.Namespace) -> int:
    narrative = _read_text_arg(args.narrative, args.narrative_file, label="narrative")
    return _run_write(
        args,
        lambda c: c.save_chronicle_current_era(
            campaign_ref=args.campaign_ref,
            expected_revision=args.revision,
            narrative=narrative,
            title=args.title,
            start_date=args.start_date,
            events_covered=args.events_covered,
        ),
    )


def cmd_chronicle_update(args: argparse.Namespace) -> int:
    narrative = _read_text_arg(args.narrative, args.narrative_file, label="narrative")
    return _run_write(
        args,
        lambda c: c.update_chronicle_chapter(
            campaign_ref=args.campaign_ref,
            expected_revision=args.revision,
            chapter_number=args.chapter,
            narrative=narrative,
            title=args.title,
            summary=args.summary,
            epigraph=args.epigraph,
        ),
    )


def cmd_chronicle_create(args: argparse.Namespace) -> int:
    narrative = _read_text_arg(args.narrative, args.narrative_file, label="narrative")
    return _run_write(
        args,
        lambda c: c.create_chronicle_chapter(
            campaign_ref=args.campaign_ref,
            expected_revision=args.revision,
            narrative=narrative,
            title=args.title,
            summary=args.summary,
            start_date=args.start_date,
            end_date=args.end_date,
            epigraph=args.epigraph,
        ),
    )


def cmd_chronicle_undo(args: argparse.Namespace) -> int:
    return _run_write(
        args,
        lambda c: c.undo_chronicle_edit(
            campaign_ref=args.campaign_ref,
            expected_revision=args.revision,
            edit_receipt=args.edit_receipt,
        ),
    )


# ------------------------------------------------------------------- doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check the things that actually break: parser binary, deps, saves, archive."""
    checks: list[tuple[bool, str, str]] = []

    try:
        from augur.parser.rust_bridge import PARSER_BINARY

        exists = Path(PARSER_BINARY).exists()
        checks.append(
            (
                exists,
                f"Rust parser: {PARSER_BINARY}",
                "Build it: cd stellaris-parser && cargo build --release",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append((False, f"Rust parser bridge failed to load: {exc}", ""))

    for module, hint in (("orjson", "pip install orjson"), ("watchdog", "pip install watchdog")):
        try:
            __import__(module)
            checks.append((True, f"Python dependency: {module}", ""))
        except ImportError:
            checks.append((False, f"Python dependency missing: {module}", hint))

    from augur.parser.save_loader import find_most_recent_save, get_platform_save_paths

    latest = find_most_recent_save()
    if latest:
        checks.append((True, f"Newest save: {latest}", ""))
    else:
        searched = ", ".join(str(p) for p in get_platform_save_paths())
        checks.append((False, f"No save found. Searched: {searched}", "Pass --save explicitly."))

    db_path = resolve_db_path(args.db)
    if db_path.exists():
        size_mb = db_path.stat().st_size / (1024 * 1024)
        snapshot_note = ""
        try:
            db = GameDatabase(db_path=db_path)
            context = CampaignContext(db=db, language=args.language)
            campaign = context.get_active_campaign()
            context.close()
            db.close()
            if campaign.get("save_loaded"):
                snapshot_note = (
                    f" — {campaign.get('empire_name')} @ {campaign.get('game_date')}, "
                    f"{campaign.get('snapshot_count')} snapshot(s)"
                )
        except Exception as exc:  # noqa: BLE001
            snapshot_note = f" — could not read: {exc}"
        checks.append((True, f"Archive: {db_path} ({size_mb:.1f} MB){snapshot_note}", ""))
    else:
        checks.append((False, f"No archive yet at {db_path}", "Run: augur ingest"))

    checks.append((True, f"Project root: {get_repo_root()}", ""))

    if args.json:
        _emit(
            {
                "ok": all(ok for ok, _, _ in checks),
                "checks": [{"ok": ok, "detail": detail, "hint": hint} for ok, detail, hint in checks],
            },
            as_json=True,
        )
    else:
        for ok, detail, hint in checks:
            print(f"[{'ok' if ok else 'XX'}] {detail}")
            if not ok and hint:
                print(f"       -> {hint}")
    return EXIT_OK if all(ok for ok, _, _ in checks) else EXIT_ERROR


# -------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="augur",
        description=(
            "Read the omens in a Stellaris save. Parses the save locally, tracks what "
            "changed between saves, and prints a briefing for whoever asked — no API "
            "key, no model client, no MCP server."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Typical session:\n"
            "  augur doctor            # check parser, deps, save folder\n"
            "  augur ingest            # read the newest save into the archive\n"
            "  augur brief -q 'who should I worry about?'\n"
            "  augur vigil             # keep ingesting while you play\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"augur {__version__}")
    parser.add_argument("--db", default=None, help="Archive path (default: $AUGUR_DB or XDG state dir).")
    parser.add_argument("--language", default="en", help="Language scope for cached content.")
    parser.add_argument("--json", action="store_true", help="Emit raw JSON instead of Markdown.")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("ingest", help="Parse a save into the archive.", aliases=["read-save"])
    p.add_argument("--save", default=None, help="Save file or directory (default: newest found).")
    p.add_argument("--no-wait", action="store_true", help="Skip the save-stability wait.")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("vigil", help="Watch the save folder and ingest continuously.", aliases=["watch"])
    p.add_argument("--save-dir", default=None, help="Directory to watch (default: platform paths).")
    p.add_argument("--render", default=None, help="Write a Markdown briefing here after each ingest.")
    p.add_argument("--no-initial", action="store_true", help="Do not ingest the existing newest save.")
    p.set_defaults(func=cmd_vigil)

    p = sub.add_parser("saves", help="List detected save files.")
    p.add_argument("--save-dir", default=None, help="Directory to search.")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_saves)

    p = sub.add_parser("status", help="Active campaign: empire, date, snapshot count.")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser(
        "brief",
        help="Full strategy context for a question. The main read command.",
        aliases=["scry"],
    )
    p.add_argument("-q", "--question", default=None, help="The question to focus the briefing on.")
    p.add_argument("--focus", default="auto", choices=FOCUS_CHOICES, help="Section focus.")
    p.add_argument("--events", type=int, default=15, help="Recent events to include.")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("detail", help="Specific briefing sections for follow-up depth.")
    p.add_argument("--sections", default=None, help="Comma-separated section names.")
    p.add_argument("--detail", default="compact", choices=["compact", "full"])
    p.set_defaults(func=cmd_detail)

    p = sub.add_parser("events", help="Detected changes between saves.", aliases=["omens"])
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--notable", action="store_true", help="Only high-signal event types.")
    p.set_defaults(func=cmd_events)

    chronicle = sub.add_parser("chronicle", help="Read and write the campaign narrative.")
    csub = chronicle.add_subparsers(dest="chronicle_command", metavar="<action>")

    c = csub.add_parser("read", help="Saved Chronicle chapters.")
    c.set_defaults(func=cmd_chronicle_read)

    c = csub.add_parser("source", help="Raw material for writing the next chapter.")
    c.add_argument("--scope", default="current_era", help="current_era | chapter | all")
    c.add_argument("--chapter", type=int, default=None)
    c.add_argument("--max-events", type=int, default=80)
    c.set_defaults(func=cmd_chronicle_source)

    def add_write_guards(target: argparse.ArgumentParser) -> None:
        # Both come from a fresh read; they stop a stale write from clobbering
        # a Chronicle that changed underneath the writer.
        target.add_argument("--campaign-ref", required=True, help="From a fresh read.")
        target.add_argument("--revision", required=True, help="chronicle_revision from a fresh read.")

    def add_narrative(target: argparse.ArgumentParser) -> None:
        target.add_argument("--narrative", default=None, help="Prose inline (short text only).")
        target.add_argument(
            "--narrative-file", default=None, help="File holding the prose, or - for stdin."
        )

    c = csub.add_parser("save", help="Write the current era as a chapter.")
    add_write_guards(c)
    add_narrative(c)
    c.add_argument("--title", default=None)
    c.add_argument("--start-date", default=None)
    c.add_argument("--events-covered", type=int, default=None)
    c.set_defaults(func=cmd_chronicle_save)

    c = csub.add_parser("update", help="Rewrite an existing chapter.")
    add_write_guards(c)
    add_narrative(c)
    c.add_argument("--chapter", type=int, required=True)
    c.add_argument("--title", default=None)
    c.add_argument("--summary", default=None)
    c.add_argument("--epigraph", default=None)
    c.set_defaults(func=cmd_chronicle_update)

    c = csub.add_parser("create", help="Add a new chapter.")
    add_write_guards(c)
    add_narrative(c)
    c.add_argument("--title", required=True)
    c.add_argument("--summary", default=None)
    c.add_argument("--start-date", default=None)
    c.add_argument("--end-date", default=None)
    c.add_argument("--epigraph", default=None)
    c.set_defaults(func=cmd_chronicle_create)

    c = csub.add_parser("undo", help="Revert the last external Chronicle edit.")
    add_write_guards(c)
    c.add_argument("--edit-receipt", required=True)
    c.set_defaults(func=cmd_chronicle_undo)

    p = sub.add_parser("doctor", help="Check parser, dependencies, saves and archive.")
    p.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "func", None):
        if getattr(args, "command", None) == "chronicle":
            print("augur chronicle: pick an action (read, source, save, update, create, undo)", file=sys.stderr)
        else:
            parser.print_help()
        return EXIT_USAGE

    try:
        return int(args.func(args))
    except ContextError as exc:
        print(f"augur: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # Downstream closed the pipe (head, less). Not an error.
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
