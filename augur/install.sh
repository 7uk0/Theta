#!/usr/bin/env bash
# Install augur: python package, Rust parser, and the Claude Code skill.
#
# By default the skill and commands are linked into ~/.claude so they work from
# any directory, not just inside this repo. Pass --repo-only to skip that.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
LINK_GLOBAL=1
[[ "${1:-}" == "--repo-only" ]] && LINK_GLOBAL=0

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }

say "Building the Rust parser (required — this is what reads the save)"
if ! command -v cargo >/dev/null 2>&1; then
  echo "cargo not found. Install Rust first: https://rustup.rs" >&2
  exit 1
fi
(cd "$HERE/stellaris-parser" && cargo build --release)

say "Installing the Python package"
python3 -m pip install -e "$HERE"

if [[ "$LINK_GLOBAL" == "1" ]]; then
  say "Linking the Claude Code skill and commands into ~/.claude"
  mkdir -p "$HOME/.claude/skills" "$HOME/.claude/commands"
  ln -sfn "$REPO_ROOT/.claude/skills/augur" "$HOME/.claude/skills/augur"
  for cmd in omen chronicle vigil; do
    ln -sfn "$REPO_ROOT/.claude/commands/$cmd.md" "$HOME/.claude/commands/$cmd.md"
  done
fi

say "Checking the installation"
augur doctor || true

cat <<'DONE'

Installed. Next:

  augur ingest                      read your newest save
  augur brief -q "how am I doing?"  or just ask Claude Code about your empire
  augur vigil                       keep reading saves while you play

In Claude Code, the augur skill triggers on any question about your Stellaris
campaign. /omen and /chronicle are there if you prefer to be explicit.
DONE
