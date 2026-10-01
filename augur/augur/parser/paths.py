"""Project-root resolution for locating bundled resources.

The Rust parser binary, the patch-notes corpus, and (in development) the
Stellaris save fixtures all live relative to the augur project root rather
than inside the installed package. Resolve that root once, here, so every
caller agrees on where to look.

Resolution order:

1. ``AUGUR_HOME`` — explicit override. Set this when augur is installed as a
   wheel but its ``stellaris-parser`` build tree lives elsewhere.
2. The nearest ancestor holding both ``pyproject.toml`` and the ``augur``
   package directory — the normal development checkout.
3. The package directory itself, for a site-packages install where no repo
   markers exist. Callers fall back to in-package resources from there.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_PROJECT_ROOT = "AUGUR_HOME"


def get_repo_root(start: Path | None = None) -> Path:
    """Best-effort augur project root detection."""
    override = os.environ.get(ENV_PROJECT_ROOT)
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_dir():
            return candidate

    here = (start or Path(__file__)).resolve()
    for parent in [here.parent, *here.parents]:
        if (parent / "pyproject.toml").exists() and (parent / "augur").is_dir():
            return parent
    # Installed without a checkout: package dir is the best anchor we have.
    return Path(__file__).resolve().parent.parent


def get_state_dir() -> Path:
    """Directory holding the campaign database and rendered briefings.

    Honours ``AUGUR_STATE_DIR``, then ``XDG_STATE_HOME``, then the XDG default.
    """
    override = os.environ.get("AUGUR_STATE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "augur"
