#!/usr/bin/env bash
# Install augur: the Rust parser, the Python package, and the Claude Code skill.
#
# Debian 13 (and every Debian since bookworm) marks the system Python as
# externally managed under PEP 668, so `pip install` into it is refused. This
# script therefore installs into a virtualenv at augur/.venv and links the
# `augur` command into ~/.local/bin, which keeps the system Python untouched and
# still puts one command on PATH.
#
#   ./install.sh                 parser + package + link the skill globally
#   ./install.sh --repo-only     skip the ~/.claude links (skill stays repo-local)
#   ./install.sh --no-venv       install with plain pip (for an env you manage)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
VENV="$HERE/.venv"
BIN_DIR="$HOME/.local/bin"
LINK_GLOBAL=1
USE_VENV=1

for arg in "$@"; do
  case "$arg" in
    --repo-only) LINK_GLOBAL=0 ;;
    --no-venv)   USE_VENV=0 ;;
    -h|--help)   sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[31mXX\033[0m  %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- Rust parser

say "Building the Rust parser (this is what reads the save)"
command -v cargo >/dev/null 2>&1 || die "cargo not found. Install Rust: https://rustup.rs"
(cd "$HERE/stellaris-parser" && cargo build --release)
PARSER="$HERE/stellaris-parser/target/release/stellaris-parser"
[[ -x "$PARSER" ]] || die "the parser did not build: $PARSER is missing"

# ------------------------------------------------------------- Python package

command -v python3 >/dev/null 2>&1 || die "python3 not found"

if [[ "$USE_VENV" == "1" ]]; then
  say "Creating a virtualenv at $VENV"
  if ! python3 -m venv "$VENV" 2>/dev/null; then
    die "could not create a virtualenv. On Debian/Ubuntu: sudo apt install python3-venv"
  fi
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  say "Installing augur into the virtualenv"
  "$VENV/bin/python" -m pip install --quiet -e "$HERE"

  mkdir -p "$BIN_DIR"
  ln -sfn "$VENV/bin/augur" "$BIN_DIR/augur"
  say "Linked $BIN_DIR/augur"
  AUGUR_CMD="$BIN_DIR/augur"

  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not on your PATH. Add this to ~/.bashrc:"
       warn '  export PATH="$HOME/.local/bin:$PATH"' ;;
  esac
else
  say "Installing augur with the current python3 (no virtualenv)"
  python3 -m pip --version >/dev/null 2>&1 || die "python3 -m pip is unavailable. On Debian: sudo apt install python3-pip"
  if ! python3 -m pip install -e "$HERE" 2>/tmp/augur-pip.log; then
    if grep -q externally-managed /tmp/augur-pip.log; then
      die "this Python is externally managed (PEP 668). Re-run without --no-venv, or use pipx."
    fi
    cat /tmp/augur-pip.log >&2
    die "pip install failed"
  fi
  AUGUR_CMD="$(command -v augur || true)"
fi

# ----------------------------------------------------- Claude Code skill links

if [[ "$LINK_GLOBAL" == "1" ]]; then
  SKILL_SRC="$REPO_ROOT/.claude/skills/augur"
  if [[ ! -d "$SKILL_SRC" ]]; then
    warn "no skill directory at $SKILL_SRC — did you clone only the augur/ folder?"
    warn "skipping the ~/.claude links; the skill works when you run claude inside the repo"
  else
    say "Linking the skill and commands into ~/.claude"
    mkdir -p "$HOME/.claude/skills" "$HOME/.claude/commands"
    DEST="$HOME/.claude/skills/augur"
    # ln -sfn into an existing REAL directory silently nests the link inside it,
    # and the skill then never loads. Refuse instead of pretending it worked.
    if [[ -d "$DEST" && ! -L "$DEST" ]]; then
      die "$DEST is a real directory. Move or remove it, then re-run."
    fi
    ln -sfn "$SKILL_SRC" "$DEST"
    [[ -f "$DEST/SKILL.md" ]] || die "the skill link is broken: $DEST/SKILL.md is not readable"
    for cmd in omen chronicle vigil; do
      SRC="$REPO_ROOT/.claude/commands/$cmd.md"
      [[ -f "$SRC" ]] && ln -sfn "$SRC" "$HOME/.claude/commands/$cmd.md"
    done
  fi
fi

# ------------------------------------------------------------------ verify

say "Checking the installation"
if [[ -z "${AUGUR_CMD:-}" ]] || ! "$AUGUR_CMD" --version >/dev/null 2>&1; then
  warn "the augur command is not runnable yet; use: $VENV/bin/augur"
  AUGUR_CMD="$VENV/bin/augur"
fi
"$AUGUR_CMD" doctor || warn "doctor reported problems — read the hints above"

cat <<DONE

Installed. Next:

  augur ingest                      read your newest save
  augur dash                        live dashboard; watches saves as you play
  augur brief -q "how am I doing?"  or just ask Claude Code about your empire

In Claude Code, the augur skill triggers on any question about your Stellaris
campaign. /omen, /chronicle and /vigil are there if you prefer to be explicit.
DONE
