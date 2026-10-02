# CLAUDE.md

Guidance for Claude Code working in this repository.

## What Theta is

Theta is the User's home lab monorepo — "All Flows". Each top-level directory is
a self-contained flow. Flows do not import from each other; they share
conventions, not code.

| Flow | What it is |
| --- | --- |
| [`augur/`](augur/) | Stellaris campaign advisor. Parses a save locally, hands state to Claude Code. |

The broader Theta project is a local AI built on OpenWebUI. This repo holds the
tooling and flows around it.

## Home lab topology

Knowing which machine a thing runs on matters more here than usual, because
most flows touch local files that only exist on one node.

| Node | Role | Notes |
| --- | --- | --- |
| **Wolf-Box** | Primary workstation | Ryzen 7 3700X, RTX 3060 Ti, Debian 13 Trixie, Xfce4/Chicago95, 3440x1440. Where games and local AI actually run. |
| **Calavara-Orbital** | Server | ThinkStation P520c, Xeon W-2135, dual Quadro P2000, 62 GiB RAM. `192.168.1.95`. SSH alias `Calavara-Orbital` (`zuko@192.168.1.95`, key `wolfbox-to-calavera`). |
| **Fox-Box** | Portable node | ThinkPad T14s Gen 6 AMD, Ryzen AI 7 PRO 350, XDNA 2 NPU. Debian 13 on an external SanDisk SSD; internal Windows untouched, boots via F12 only. |

## Working in a cloud session (read this first)

Sessions are often driven from a phone against a cloud container, not from
Wolf-Box. That has hard consequences:

- **The container cannot see Wolf-Box.** No Stellaris saves, no Ollama, no local
  services, no `~/.local/state` from the workstation. A cloud session can write
  and test code; it cannot run a flow against real local data.
- **The container is ephemeral.** It is reclaimed after inactivity. Anything not
  committed and pushed is gone. There is no memory directory — this file *is*
  the memory. Durable context goes here, in a flow's README, or in a docs file,
  and gets pushed.
- **Verify what you can, and say what you could not.** Building a synthetic
  fixture to exercise a pipeline is the right move when the real input lives on
  another machine. State plainly which parts were run and which were not.

Development branch for cloud sessions: `ccr-*` as assigned per session. Never
push to `main` directly.

## augur

A cannibalized fork of
[gitmaan/stellaris-companion](https://github.com/gitmaan/stellaris-companion)
(MIT, carried forward in `augur/LICENSE-stellaris-companion`).

**Why it exists:** upstream is good but needs a paid model endpoint (Gemini,
OpenRouter) or an MCP server bridged into a desktop client. Paying for an API
key for a hobby project is not worth it. `augur` keeps the hard part — reading
the save — and prints state to stdout so Claude Code does the reasoning on the
existing subscription.

**Architecture:**

```
.sav (zip: gamestate + meta)
  -> stellaris-parser (Rust)   parse once, many queries over stdin
  -> augur/extractor/          military, economy, diplomacy, planets, tech, ...
  -> augur/core/ signals+history   resolved names, diffable state
  -> campaign.db (SQLite)      snapshots + detected events + Chronicle
  -> augur/context.py          the tool surface
  -> augur/cli.py -> stdout    Claude Code reasons over it
  -> augur/dash.py             curses dashboard over the same context
```

`context.py` is upstream's MCP tool layer with the JSON-RPC transport removed.
It is the seam the whole fork hangs on — ten methods, no transport coupling.
Keep it that way: the CLI is one caller, not the owner.

**Where things live:**

| What | Where | Override |
| --- | --- | --- |
| Campaign archive | `~/.local/state/augur/campaign.db` | `--db`, `$AUGUR_DB` |
| State dir | `~/.local/state/augur/` | `$AUGUR_STATE_DIR` |
| Project root (parser, patches) | `augur/` | `$AUGUR_HOME` |

**Non-negotiables:**

- **No model client, ever.** No API keys, no provider SDKs, no `google-genai`,
  no `openai`. Dependencies are `orjson` and `watchdog`; adding a third needs a
  real reason. The whole point of the fork is that Claude Code is the model.
- **No MCP server.** The commands are the interface.
- The Rust parser must be built before anything works:
  `cd augur/stellaris-parser && cargo build --release`. `augur doctor` checks it.
- `augur vigil` and `augur dash` run until interrupted. Never start either in
  the foreground of a session; tell the User to run them in their own terminal.
- The dashboard is stdlib `curses` on purpose — no `rich`, no `textual`. Its
  pure parts (`build_state`, `extract_nets`, `sparkline`, `collect_alerts`) are
  unit-tested; the curses path is covered by `scripts/smoke_dash.py`, which
  drives the real command in a pty. Add to both when changing it.
- Chronicle writes need `campaign_ref` + `chronicle_revision` from a *fresh*
  read. They are concurrency guards. Never write without an explicit request.

**Gotchas learned the hard way:**

- Event detection is a diff between snapshots. One ingest produces zero events;
  this is correct, not a bug.
- Extractor field coverage against a real late-game save is **unvalidated** in
  this fork. If economy nets or pop counts come back zero on a real save, suspect
  the extractor's expected save structure before suspecting the CLI.
  `AUGUR_TEST_SAVE=path python3 -m pytest -m integration` runs the real pipeline.
- `get_empire_briefing` nests its payload under a `sections` key while
  `get_strategy_context` returns sections at the top level, and monthly nets
  appear in three different shapes. Use `augur.dash.unwrap_sections` and
  `extract_nets` rather than reaching into either shape directly.
- Upstream strings referenced an Electron app and a Chronicle page that no
  longer exist. Several were fixed; if more surface in output, fix them rather
  than passing them through to the User.

## Conventions

**Naming.** Component and command names lean esoteric — divinatory, alchemical,
psychedelic — where it does not cost clarity. `augur` reads omens in the
heavens, which is what parsing a star empire's save amounts to. Subcommands stay
plainly named so they are guessable, with flavoured aliases where they help
(`scry` → `brief`, `omens` → `events`, `vigil` → `watch`). Flavour never wins
over a name someone has to type correctly under pressure.

**Tests.** Every flow carries its own tests and runs them with `pytest` from the
flow directory. Tests that need hardware or local data get
`@pytest.mark.integration` and skip cleanly without it.

**Lint.** `ruff check .` per flow. Fix the cause, don't add a per-file ignore,
unless the file is vendored upstream code.

**Ported code.** When cannibalizing an upstream project: compute the import
closure with an AST walk rather than guessing, rewrite the namespace
mechanically, then port the upstream tests for the kept surface. Credit the
original and carry its license file.
