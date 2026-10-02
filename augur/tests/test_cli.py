"""End-to-end tests for the command surface that replaced the MCP server.

Each test drives `main()` the way Claude Code does — argv in, stdout out, exit
code checked — against a synthetic archive, so the whole path (argparse ->
context -> renderer) is exercised without needing a real 200MB save.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from augur.cli import (
    EXIT_ERROR,
    EXIT_NO_CAMPAIGN,
    EXIT_OK,
    EXIT_USAGE,
    main,
    resolve_db_path,
)
from tests.test_context import _make_test_db


@pytest.fixture()
def archive(tmp_path: Path) -> Path:
    """A populated archive on disk, closed so the CLI can open it itself."""
    db, _, _ = _make_test_db(tmp_path)
    path = db.path
    db.close()
    return path


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_status_renders_markdown(archive: Path, capsys) -> None:
    code, out, _ = run(capsys, "--db", str(archive), "status")

    assert code == EXIT_OK
    assert out.startswith("# Kilik Cooperative — 2235.04.01")
    assert "Snapshot count:** 2" in out


def test_status_json_is_machine_readable(archive: Path, capsys) -> None:
    code, out, _ = run(capsys, "--db", str(archive), "--json", "status")

    assert code == EXIT_OK
    payload = json.loads(out)
    assert payload["save_loaded"] is True
    assert payload["empire_name"] == "Kilik Cooperative"


def test_brief_includes_question_focus_and_sections(archive: Path, capsys) -> None:
    code, out, _ = run(
        capsys, "--db", str(archive), "brief", "-q", "Can I afford this war?", "--focus", "economy"
    )

    assert code == EXIT_OK
    assert "Can I afford this war?" in out
    assert "**Focus:** economy" in out
    assert "## Briefing" in out
    # The campaign_ref is the write guard; a reader must be able to find it.
    assert "campaign:" in out.lower() or "Campaign ref" in out


def test_brief_json_carries_no_model_call(archive: Path, capsys) -> None:
    code, out, _ = run(capsys, "--db", str(archive), "--json", "brief", "-q", "status?")

    assert code == EXIT_OK
    payload = json.loads(out)
    # The point of the fork: context is returned for the caller to reason over.
    assert "No in-app LLM call was made" in payload["advisor_mode"]
    assert payload["privacy"]["raw_save_file_included"] is False
    assert payload["briefing"]


def test_events_renders_dated_bullets(archive: Path, capsys) -> None:
    code, out, _ = run(capsys, "--db", str(archive), "events", "--limit", "5")

    assert code == EXIT_OK
    assert "# Recent events" in out
    # The context layer converts raw event types to display names before output.
    assert "war started" in out.lower()


def test_detail_honours_the_section_filter(archive: Path, capsys) -> None:
    code, out, _ = run(
        capsys, "--db", str(archive), "--json", "detail", "--sections", "economy,military"
    )

    assert code == EXIT_OK
    payload = json.loads(out)
    # The filter must actually narrow the payload; `assert payload` would pass
    # even if --sections were ignored and every section came back.
    assert set(payload["sections"]) == {"economy", "military"}
    assert payload["sections"]["military"]["military_power"] == 4200


def test_chronicle_read_renders_chapters(archive: Path, capsys) -> None:
    code, out, _ = run(capsys, "--db", str(archive), "chronicle", "read")

    assert code == EXIT_OK
    assert "First Light Beyond the Rim" in out


def test_chronicle_source_carries_the_write_guards(archive: Path, capsys) -> None:
    code, out, _ = run(capsys, "--db", str(archive), "--json", "chronicle", "source")

    assert code == EXIT_OK
    payload = json.loads(out)
    # This is step 1 of the documented write sequence; it is only useful if the
    # two guards a write needs come back with it.
    assert payload["campaign_ref"]
    assert payload["chronicle_revision"]


def test_chronicle_create_from_file_then_undo(archive: Path, capsys, tmp_path: Path) -> None:
    """The write path Claude Code uses: prose to a file, guards from a fresh read."""
    _, out, _ = run(capsys, "--db", str(archive), "--json", "chronicle", "read")
    state = json.loads(out)
    campaign_ref = state["campaign_ref"]
    revision = state["chronicle_revision"]

    narrative = tmp_path / "chapter.md"
    narrative.write_text("The deficit held, and the fleets came home.", encoding="utf-8")

    code, out, _ = run(
        capsys,
        "--db",
        str(archive),
        "chronicle",
        "create",
        "--campaign-ref",
        campaign_ref,
        "--revision",
        revision,
        "--title",
        "Ledgers of the Long Retreat",
        "--narrative-file",
        str(narrative),
    )
    assert code == EXIT_OK
    created = json.loads(out)
    receipt = created["edit_receipt"]
    new_revision = created["chronicle_revision"]

    code, out, _ = run(
        capsys,
        "--db",
        str(archive),
        "chronicle",
        "undo",
        "--campaign-ref",
        campaign_ref,
        "--revision",
        new_revision,
        "--edit-receipt",
        receipt,
    )
    assert code == EXIT_OK

    _, out, _ = run(capsys, "--db", str(archive), "chronicle", "read")
    assert "Ledgers of the Long Retreat" not in out


def test_chronicle_write_rejects_stale_revision(archive: Path, capsys, tmp_path: Path) -> None:
    narrative = tmp_path / "chapter.md"
    narrative.write_text("Stale writers must not clobber.", encoding="utf-8")

    code, _, err = run(
        capsys,
        "--db",
        str(archive),
        "chronicle",
        "create",
        "--campaign-ref",
        "campaign:save-mcp",
        "--revision",
        "obviously-stale",
        "--title",
        "Should Not Land",
        "--narrative-file",
        str(narrative),
    )

    assert code != EXIT_OK
    assert "augur:" in err


def test_narrative_requires_exactly_one_source(archive: Path, capsys, tmp_path: Path) -> None:
    code, _, err = run(
        capsys,
        "--db",
        str(archive),
        "chronicle",
        "create",
        "--campaign-ref",
        "campaign:save-mcp",
        "--revision",
        "whatever",
        "--title",
        "No Prose",
    )

    assert code != EXIT_OK
    assert "narrative" in err


def test_missing_archive_reports_distinct_exit_code(tmp_path: Path, capsys) -> None:
    """Exit 3 lets a caller tell 'not ingested yet' from a real failure."""
    code, _, err = run(capsys, "--db", str(tmp_path / "absent.db"), "status")

    assert code == EXIT_NO_CAMPAIGN
    assert "augur ingest" in err


def test_bare_invocation_prints_help(capsys) -> None:
    code, out, _ = run(capsys)

    assert code == EXIT_USAGE
    # "augur" alone cannot fail — prog is in every usage line. Assert the command
    # list is actually there.
    assert "<command>" in out
    for command in ("ingest", "brief", "chronicle", "doctor", "dash"):
        assert command in out


def test_chronicle_without_action_explains_itself(capsys) -> None:
    code, _, err = run(capsys, "chronicle")

    assert code == EXIT_USAGE
    assert "read" in err


def test_db_path_precedence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUGUR_DB", str(tmp_path / "from-env.db"))
    assert resolve_db_path(None) == tmp_path / "from-env.db"
    # An explicit flag always wins over the environment.
    assert resolve_db_path(str(tmp_path / "explicit.db")) == tmp_path / "explicit.db"

    monkeypatch.delenv("AUGUR_DB")
    monkeypatch.setenv("AUGUR_STATE_DIR", str(tmp_path / "state"))
    assert resolve_db_path(None) == tmp_path / "state" / "campaign.db"


def test_doctor_runs_without_an_archive(tmp_path: Path, capsys) -> None:
    code, out, _ = run(capsys, "--db", str(tmp_path / "none.db"), "--json", "doctor")

    report = json.loads(out)
    assert any("archive" in c["detail"].lower() for c in report["checks"])
    # Missing archive is a failed check, so doctor exits non-zero. That is the contract.
    assert code != EXIT_OK


# --- regressions from the bug-hunt pass


@pytest.mark.parametrize(
    "argv",
    [
        ["status", "--json"],
        ["--json", "status"],
        ["brief", "--json"],
        ["--json", "brief"],
        ["chronicle", "read", "--json"],
        ["--json", "chronicle", "read"],
        ["chronicle", "--json", "read"],
    ],
)
def test_global_flags_work_on_either_side_of_the_subcommand(
    archive: Path, capsys, argv: list[str]
) -> None:
    """`augur brief --json` is what anyone types; it used to exit 2."""
    code, out, _ = run(capsys, "--db", str(archive), *argv)

    assert code == EXIT_OK, f"{argv} was rejected"
    json.loads(out)  # raises if Markdown came back instead


def test_db_flag_is_accepted_after_the_subcommand(tmp_path: Path, capsys) -> None:
    code, _, err = run(capsys, "status", "--db", str(tmp_path / "absent.db"))

    assert code == EXIT_NO_CAMPAIGN
    assert "augur ingest" in err


def test_a_bad_request_does_not_borrow_the_no_campaign_exit_code(
    archive: Path, capsys
) -> None:
    """Exit 3 means "run an ingest"; a wrong --chapter is just an error."""
    code, _, err = run(
        capsys, "--db", str(archive), "chronicle", "source", "--scope", "chapter", "--chapter", "99"
    )

    assert code == EXIT_ERROR
    assert code != EXIT_NO_CAMPAIGN
    assert "augur:" in err


def test_corrupt_archive_explains_itself_instead_of_raising(tmp_path: Path, capsys) -> None:
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a database")

    code, _, err = run(capsys, "--db", str(corrupt), "status")

    assert code == EXIT_ERROR
    assert "not a readable campaign archive" in err
    assert "Traceback" not in err


def test_doctor_fails_on_an_unreadable_archive(tmp_path: Path, capsys) -> None:
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a database")

    code, out, _ = run(capsys, "--db", str(corrupt), "--json", "doctor")

    report = json.loads(out)
    archive_check = next(c for c in report["checks"] if "Archive:" in c["detail"])
    # doctor exists to surface exactly this; reporting ok with a note is useless.
    assert archive_check["ok"] is False
    assert code != EXIT_OK


def test_saves_rejects_a_limit_below_one(tmp_path: Path, capsys) -> None:
    """--limit 0 used to claim no saves existed at all."""
    save = tmp_path / "a.sav"
    import zipfile

    with zipfile.ZipFile(save, "w") as archive_zip:
        archive_zip.writestr("gamestate", "date=\"2200.01.01\"")
        archive_zip.writestr("meta", "version=\"x\"")

    code, _, err = run(capsys, "saves", "--save-dir", str(tmp_path), "--limit", "0")

    assert code == EXIT_USAGE
    assert "at least 1" in err


def test_chronicle_prose_is_not_printed_twice(archive: Path, capsys, tmp_path: Path) -> None:
    """Chapters carry `narrative` plus a `sections` prose copy of the same text."""
    _, out, _ = run(capsys, "--db", str(archive), "--json", "chronicle", "read")
    state = json.loads(out)
    narrative = tmp_path / "ch.md"
    narrative.write_text("A singular and unrepeated sentence.", encoding="utf-8")

    run(
        capsys, "--db", str(archive), "chronicle", "create",
        "--campaign-ref", state["campaign_ref"],
        "--revision", state["chronicle_revision"],
        "--title", "Echo Test",
        "--narrative-file", str(narrative),
    )

    _, out, _ = run(capsys, "--db", str(archive), "chronicle", "read")
    assert out.count("A singular and unrepeated sentence.") == 1
    assert "### \n" not in out


def test_knowledge_uses_the_version_recorded_in_the_archive(archive: Path, capsys) -> None:
    """The patch-notes corpus was dead code; this is the path that makes it live."""
    code, out, _ = run(capsys, "--db", str(archive), "knowledge")

    assert code == EXIT_OK
    assert "Evidence hierarchy" in out
    # The fixture campaign records "Corvus v4.2.4", so 4.2 mechanics are selected.
    assert "4.2" in out


def test_knowledge_accepts_an_explicit_version_without_an_archive(
    tmp_path: Path, capsys
) -> None:
    code, out, _ = run(
        capsys, "--db", str(tmp_path / "absent.db"), "knowledge", "--game-version", "4.5.1"
    )

    assert code == EXIT_OK
    assert "Evidence hierarchy" in out


def test_knowledge_says_what_it_needs_when_the_version_is_unknown(
    tmp_path: Path, capsys
) -> None:
    code, _, err = run(capsys, "--db", str(tmp_path / "absent.db"), "knowledge")

    assert code == EXIT_NO_CAMPAIGN
    assert "--game-version" in err
    # It should list what it actually has, rather than leaving the user guessing.
    assert "4.5.1" in err


def test_knowledge_purpose_changes_the_rules_it_states(archive: Path, capsys) -> None:
    _, advisor, _ = run(capsys, "--db", str(archive), "knowledge", "--purpose", "advisor")
    _, chronicle, _ = run(capsys, "--db", str(archive), "knowledge", "--purpose", "chronicle")

    assert advisor != chronicle
    assert "form advice" in advisor
    assert "never as evidence that an event" in chronicle
