# Port notes

What was taken from
[gitmaan/stellaris-companion](https://github.com/gitmaan/stellaris-companion),
what was left behind, and why. Written so nobody has to re-derive it — including
the question "why isn't there an X any more", which usually has an answer here.

Upstream commit at the time of the fork: `84274dc`
("feat: improve MCP setup, Chronicle sharing, and Cygnus guidance (#55)").

## The decision

Upstream has two consumers of the same campaign data:

- `backend/core/companion.py` — calls a model provider (Gemini by default,
  also OpenRouter / Ollama / LM Studio) with the user's API key.
- `backend/mcp/server.py` — serves the data over MCP to an external client,
  which does the reasoning instead. Its payload even says so:
  *"External client should reason from this context. No in-app LLM call was made."*

The second path needs no API key. It was built for Claude Desktop and Claude
Code over MCP. Strip the JSON-RPC transport off it and you have a local library
with ten methods; put a CLI on that and Claude Code reaches it through Bash with
no server, no configuration and no key.

That is the entire fork. Everything else follows from it.

## How the cut was decided

Not by reading directory names. The import closure was computed with an AST walk
from the methods worth keeping:

```
seeds: mcp.context, save_extractor, core.history, core.signals, core.utils,
       core.snapshot_reader, personality, game_knowledge, save_loader,
       core.save_watcher, core.ingestion_worker
```

The closure came back at **40 Python files** (44 in the package today, after the files added below) needing exactly two third-party
packages: `orjson` and `watchdog`. Everything in `pyproject.toml` beyond those —
`google-genai`, `google-auth`, `mcp`, `fastapi`, `uvicorn`, `pydantic`,
`httpx`, `python-dotenv` — fell outside it. That is the signal that the paid-API
surface was genuinely separable rather than tangled through the extractor.

## Kept

| Upstream | Here | Why |
| --- | --- | --- |
| `stellaris-parser/` | same | The Rust Clausewitz parser. A late-game save is hundreds of MB of text; Python alone is too slow. Session mode (parse once, many queries) is what makes a full briefing take seconds. |
| `stellaris_save_extractor/` | `augur/extractor/` | ~14k lines of domain knowledge about Stellaris save structure. This is the asset. Reproducing it would be the project. |
| `backend/core/{database,history,events,signals,snapshot_reader}.py` | `augur/core/` | Snapshot storage and between-save diffing. Produces the "what changed since last session" answers. |
| `backend/mcp/context.py` | `augur/context.py` | The ten tools. Renamed `StellarisMcpContext` → `CampaignContext`, `McpContextError` → `ContextError`. Transport-free already. |
| `stellaris_companion/{rust_bridge,paths,save_loader,date_utils}.py` | `augur/parser/` | Parser subprocess bridge and platform save-path detection. |
| `stellaris_companion/{personality,game_knowledge}.py` + `patches/` | `augur/knowledge/` | Derives advisor voice from the empire's ethics, authority and civics, and holds the patch-notes corpus so advice matches the installed version. |
| `backend/core/save_watcher.py` | `augur/core/` | watchdog wrapper. Works with a plain sync callback; no event loop needed. |
| `tests/test_mcp_context.py` | `tests/test_context.py` | Ported minus the JSON-RPC transport tests. The synthetic-DB fixture in it is the backbone of the new CLI tests too. |

## Cut

| Upstream | Why |
| --- | --- |
| `electron/` (~25k lines) | Desktop app, React renderer, 8-language i18n, auto-updater, tray, onboarding. The CLI is the UI now. |
| `backend/api/server.py` | FastAPI server existing only so the Electron renderer could reach Python. No renderer, no need. |
| `backend/core/companion.py` | The API-model advisor. This is the thing being replaced. Note it imported `google.genai` at module level, so it could not even be loaded without the SDK installed. |
| `backend/core/advisor_providers.py`, `model_routing.py`, `model_briefing.py` | Provider abstraction and model selection across Gemini / OpenRouter / Ollama / LM Studio. Meaningless with one fixed consumer. |
| `backend/core/chronicle.py` (~2.1k lines) | The API-model narrative generator. Claude writes the prose now. **The storage and write-back guards were kept** — they live in `context.py`, not here. |
| `backend/mcp/server.py`, `mcpb/` | The MCP server and its Claude Desktop bundle. The point of the fork. |
| `cloudflare/`, `workers/` | Discord `/ask` relay and feedback-collection worker. Both are hosted services; neither is wanted. |
| `backend/core/conversation.py`, `reporting.py` | Chat history for the in-app model, and opt-in issue reporting to the upstream worker. |
| `backend/core/ingestion_worker.py` | The cancellable subprocess worker. Its pipeline lives on in `augur/ingest.py`, run straight through instead of in a child process. |
| `backend/core/ingestion.py` | The threaded ingestion manager: latest-only scheduling, subprocess cancellation, stability windows, staleness marking. All of it exists because a GUI cannot block for 90 seconds. A CLI can. Replaced by `augur/ingest.py`, which is the same pipeline run straight through. |
| `stellaris-backend.spec`, `scripts/build-*.sh`, `scripts/publish-*`, `announcements.json` | PyInstaller, electron-builder, macOS notarization, release manifests, in-app announcements. |
| `qa_export.py`, `qa_check.py` | Upstream QA tooling against their own fixtures. `validation.py` was kept in the tree as it may be useful later. |

## Added

| File | Why |
| --- | --- |
| `augur/cli.py` | The command surface. One subcommand per former MCP tool. Exit codes are meaningful: `3` means "nothing ingested yet", distinct from `1` "something broke", so a caller can tell them apart without parsing text. |
| `augur/ingest.py` | Standalone ingestion. Includes the save-stability guard worth keeping from upstream's manager: Stellaris writes the zip in place, and parsing mid-write gets you a truncated archive, so wait for size+mtime to settle and confirm both `gamestate` and `meta` are present. |
| `augur/watch.py` | The vigil. Coalesces a burst of write events into one ingest, newest-wins, and survives a bad save rather than dying on it. |
| `augur/dash.py` | The curses dashboard — what the Electron app was for, on stdlib `curses` so no dependency was added. Its pure parts are unit-tested; the curses path is driven in a pty by `scripts/smoke_dash.py`. |
| `augur/render.py` | JSON → Markdown. Roughly halves the token cost of handing a briefing to a model and reads better. Deliberately generic — headings from sections, tables where uniform records appear, bullets elsewhere — so an extractor change adds fields rather than breaking output. |
| `install.sh` | Builds the parser, installs into a virtualenv (Debian 13 refuses a system pip install under PEP 668), links `augur` into `~/.local/bin` and the skill into `~/.claude`. |
| `../.claude/commands/{omen,chronicle,vigil}.md` | Slash commands for the explicit route. |
| `../.claude/skills/augur/SKILL.md` | Where the advisor persona went. Upstream split it between `companion.py`'s system prompt and the MCP server's `SERVER_INSTRUCTIONS`. Both targeted an external reasoner, so both collapse into one skill: persona, factual-accuracy contract, naval-capacity trap, Chronicle write protocol. |

## Fixed in passing

- `save_watcher.py` carried `PROJECT_ROOT = Path(__file__).parent.parent.parent`
  plus a `sys.path.insert`, a holdover from when shared modules sat at repo root.
  After the move it pointed at the wrong directory. Removed — `augur` is a real
  package.
- `events.py` had an import stranded mid-file, below a dataclass. Hoisted.
- `paths.py` looked for `pyproject.toml` + a `backend/` directory to find the
  repo root. Rewritten for this layout, with `$AUGUR_HOME` as an explicit
  override for non-editable installs, and `get_state_dir()` added for XDG state.
- Fourteen user-facing strings still told the reader to look at a Chronicle page
  in the Electron app, or said content was saved "to Stellaris Companion". These
  reach the User through Claude's answers, so they were reworded to describe the
  archive and the commands that actually exist.

## Verification status

Exercised end to end against a hand-built minimal save (a zip of `gamestate` and
`meta` in Clausewitz format):

- parse → extract → snapshot → persist; re-ingesting the same save correctly
  records no second snapshot
- two saves with an advanced date produced a real diff —
  `Military power: 4,250 → 6,900 (+2,650, +62%)` — confirming the
  signals/history/events chain survived the namespace move
- briefing render including automatic table detection on planet lists
- Chronicle create → read → undo through the CLI, including rejection of a stale
  revision and a first-ever chapter against an empty Chronicle
- the vigil picking up a save dropped into the watched folder, re-ingesting,
  re-rendering, and shutting down cleanly on signal

**Not verified:** field coverage against a real late-game save. The synthetic
fixture's `budget` block does not match what the extractor expects, so economy
nets came back zero — expected for a fake save, but it means real-save coverage
is unconfirmed here. The extractor is upstream's and well tested there.

```bash
AUGUR_TEST_SAVE=~/path/to/real.sav python3 -m pytest -m integration
```
