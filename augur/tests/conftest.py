"""Shared pytest configuration and fixtures.

Several tests ported from upstream expect a real Stellaris save. There is no
`.sav` in the repository (they are large and personal), so those tests skip
unless one is pointed at explicitly:

    AUGUR_TEST_SAVE=~/path/to/save.sav python3 -m pytest -m integration
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "slow: deselect with -m 'not slow'")
    config.addinivalue_line("markers", "integration: needs a real .sav and the built parser")


@pytest.fixture(scope="session")
def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def test_save_path(project_root: Path) -> str:
    """A real save to extract from, or a skip.

    `$AUGUR_TEST_SAVE` wins; otherwise a `test_save.sav` dropped at the project
    root is used, matching how upstream's tests were run.
    """
    env = os.environ.get("AUGUR_TEST_SAVE")
    if env and Path(env).expanduser().exists():
        return str(Path(env).expanduser())
    local = project_root / "test_save.sav"
    if local.exists():
        return str(local)
    pytest.skip("set AUGUR_TEST_SAVE to a .sav path to run save-backed tests")
