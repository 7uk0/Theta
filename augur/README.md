# augur

Read the omens in a Stellaris save.

`augur` parses your save locally, tracks what changed between saves, and hands
the campaign state to **Claude Code** — which does the advising. No API key, no
model subscription beyond the one you already have, no MCP server, no Electron
app, no background service you have to remember to quit.

It is a cannibalized fork of
[gitmaan/stellaris-companion](https://github.com/gitmaan/stellaris-companion)
(MIT — see `LICENSE-stellaris-companion`). That project is excellent and does
much more than this; it also needs a paid model endpoint to do the thinking.
This fork keeps the hard part — the save parsing and extraction, which is
genuinely a lot of work — and replaces the model plumbing with a shell command.

## What this is for

Ask Claude Code about your empire and have it answer from your actual save:

> "give me a strategic briefing"
> "who should I be worried about right now?"
> "what's hurting my economy?"
> "should I push alloys or research this decade?"
> "what happened since I last played?"
> "write the next Chronicle chapter"

The skill in `.claude/skills/augur/` triggers on questions like these. It runs
the CLI, reads the briefing, and answers in your empire's voice.

## Install

Needs Python 3.10+ and a Rust toolchain ([rustup](https://rustup.rs) — the
parser is Rust because a late-game save is hundreds of megabytes of
Clausewitz text and Python alone is too slow).

```bash
cd ~/Theta/augur
./install.sh
```

That builds the parser, installs the package, links the skill and commands into
`~/.claude` so they work from any directory, and runs a health check. Then:

```bash
augur ingest                        # read your newest save
augur brief -q "how am I doing?"    # or just ask Claude Code
```

## Using it

**While playing**, run the dashboard in one terminal:

```bash
augur dash
```

```
 augur · Smoke Test Collective · 2250.06.01 · Phoenix v4.5.1 ─────────────────────
  AT WAR (1)   DEFICIT: Energy
  EMPIRE                          RESOURCES
  Phase        Mid                Energy            5,400       -42/mo
  At war       yes                Minerals          3,100      +110/mo
  Colonies     11                 Alloys              980       +25/mo
  Pops         240
  Fleet power  18,200
  Fleets       6
  Techs        134
 TRENDS ─────────────────────────────────────────────────────────────────────────
  Military power     18,200  ↑ ▁▅█  +102%
  Colonies               11  ↑ ▁▅█   +22%
  Energy net            -42  → ▅▅▅    +0%
 EVENTS ─────────────────────────────────────────────────────────────────────────
  2250.06.01  War Started      The Collective entered the Rimward War.
 vigil running · 2250.06.01 (new snapshot, 4.1s)   [r]e-read [j/k] scroll [q]uit
```

It watches the save folder itself, so every autosave is parsed and the panes
update as you play. Alerts across the top are the things worth shouting: at war,
resource deficits, a crisis, a fleet lost. Trends are real snapshot history, not
a guess.

Then ask questions in a second terminal (`claude`). The dashboard tells you
*that* something changed; the conversation tells you what to do about it.

Keys: `r` re-read the newest save now, `j`/`k` scroll events, `q` quit.

If you would rather not have a dashboard, `augur vigil` does the ingesting with
no UI, and `--render PATH` writes a Markdown briefing after each save so even a
client that can only read files has something current:

```bash
augur vigil --render ~/.local/state/augur/latest.md
```

Run `augur dash --no-watch` to display an archive that a separate `vigil` is
filling.

**In Claude Code**, just ask. Or be explicit:

- `/omen <question>` — strategic briefing
- `/chronicle` — read or continue the campaign narrative
- `/vigil` — set up continuous ingestion

## Commands

| Command | What it does |
| --- | --- |
| `augur doctor` | Check parser, dependencies, save folder, archive. Start here when something is wrong. |
| `augur ingest [--save PATH]` | Parse a save into the archive. Defaults to the newest save it can find. |
| `augur dash` | **Live dashboard.** Status, resources, trends, events; watches and ingests as you play. Runs until you press `q`. |
| `augur vigil [--render PATH]` | Headless version of the same watching, no UI. Runs until interrupted. |
| `augur saves` | List detected save files. |
| `augur status` | Which campaign is loaded, which date, how fresh. |
| `augur brief -q "..."` | **The main one.** Full strategy context for a question. |
| `augur detail --sections a,b` | Specific briefing sections, for follow-up depth. |
| `augur events [--notable]` | What changed between saves. |
| `augur chronicle read\|source\|save\|update\|create\|undo` | The campaign narrative. |

Markdown by default; `--json` for raw payloads. Exit codes: `0` ok, `1` error,
`2` usage, `3` nothing ingested yet — so a caller can tell "no data" from
"broken".

### Where things live

| What | Where | Override |
| --- | --- | --- |
| Campaign archive (SQLite) | `~/.local/state/augur/campaign.db` | `--db`, `$AUGUR_DB` |
| State directory | `~/.local/state/augur/` | `$AUGUR_STATE_DIR` |
| Project root (parser, patch notes) | this directory | `$AUGUR_HOME` |
| Save files | platform defaults, auto-detected | `--save`, `--save-dir` |

Your `.sav` files are never modified, and nothing leaves the machine except
what you choose to put in a Claude Code conversation.

## How it works

```
   your .sav (zip: gamestate + meta)
        |
        v
   stellaris-parser (Rust)        parse once, answer many queries over stdin
        |
        v
   augur.extractor                military, economy, diplomacy, planets,
        |                         leaders, tech, species, endgame, geography
        v
   augur.core.signals + history   resolved names, diffable state
        |
        v
   campaign.db (SQLite)           snapshots + detected events + Chronicle
        |
        v
   augur.context                  the tool surface: status / brief / detail /
        |                         events / chronicle
        v
   augur.cli  ->  stdout  ->  Claude Code reasons over it
```

The last arrow is the whole change. Upstream it was
`backend/mcp/server.py` speaking JSON-RPC to an MCP client, or
`backend/core/companion.py` calling Gemini with your API key. Here it is a
process writing Markdown to stdout, and the thing reading it is the assistant
you are already talking to.

### What came across, what didn't

**Kept** (~32k lines, essentially unchanged):

- `stellaris-parser/` — the Rust Clausewitz parser, including session mode
- `augur/extractor/` — the whole extraction suite
- `augur/core/` — database, history, event detection, signals, snapshot reader
- `augur/context.py` — the ten tools the MCP server exposed, transport removed
- `augur/knowledge/` — empire personality derivation and the patch-notes corpus

**Cut** (~26k lines):

- `electron/` — the desktop app, renderer, 8-language i18n, auto-updater
- `cloudflare/`, `workers/` — Discord relay and feedback worker
- `backend/api/` — the FastAPI server the Electron app talked to
- `backend/core/companion.py`, `advisor_providers.py`, `model_routing.py` —
  Gemini / OpenRouter / Ollama / LM Studio client code and model selection
- `backend/core/chronicle.py` — the API-model narrative generator (Claude writes
  the prose now; the storage and write-back guards were kept)
- `backend/mcp/` — the MCP server, `mcpb/` bundle, and Claude Desktop config plumbing
- release packaging: PyInstaller spec, electron-builder, notarization, announcements

**Added**:

- `augur/cli.py` — the command surface, one subcommand per former MCP tool
- `augur/ingest.py` — standalone ingestion; upstream this was spread across an
  Electron main process, a FastAPI backend, a threaded manager and a
  cancellable worker subprocess, because a GUI cannot block. A CLI can.
- `augur/watch.py` — the save-folder vigil
- `augur/render.py` — JSON → Markdown, roughly halving the token cost
- `augur/dash.py` — the curses dashboard, replacing what the Electron app was
  for (watching state change without asking) on stdlib `curses` alone
- `.claude/skills/augur/` — the advisor persona, factual-accuracy contract and
  Chronicle write protocol, carried over from the system prompt and the MCP
  server instructions

Dependencies went from ten (including `google-genai`, `mcp`, `fastapi`,
`uvicorn`, `pydantic`) to two: `orjson` and `watchdog`.

## Where it has to run

`augur` reads save files, so it runs on the machine holding them — Wolf-Box, not
a cloud container. A Claude Code session driven from a phone cannot see those
saves. Two ways to work:

- **Claude Code on Wolf-Box** — the intended setup. `augur dash` in one terminal,
  `claude` in another. The skill triggers, the CLI runs locally, nothing crosses
  the network.
- **From a phone** — the container has no access to the saves. See
  `docs/PORT-NOTES.md` for what that constrains.

## Tests

```bash
python3 -m pytest
```

The context tests are ported from upstream, minus the JSON-RPC transport tests.
The CLI, renderer and ingest tests are new. The full parse pipeline is marked
`integration` and needs a real save:

```bash
AUGUR_TEST_SAVE=~/path/to/save.sav python3 -m pytest -m integration
```

Curses rendering is covered separately, by driving the real command in a pty:

```bash
python3 scripts/smoke_dash.py                       # synthetic archive
python3 scripts/smoke_dash.py --rows 6 --cols 30    # cramped terminal
python3 scripts/smoke_dash.py --db ~/.local/state/augur/campaign.db
```

## Known limits

- **Not validated against a real late-game save.** The pipeline is verified end
  to end against a hand-built minimal save; the extractor itself is upstream's,
  unchanged, and well tested there, but field coverage on a 300-year 1000-star
  galaxy has not been re-checked here.
- **Chronicle generation is manual now.** Upstream generated chapters on a
  schedule. Here you ask, Claude drafts, you approve the save.
- **One campaign at a time.** The archive tracks the active session the way
  upstream did. Multiple concurrent campaigns share a database but only the
  current one is read.
