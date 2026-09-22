"""Shared fixtures.

daemon/paths.py resolves its locations from the environment at import time,
so the XDG variables are redirected here -- before conftest imports anything
from the package -- and every test then runs against a scratch directory
instead of the developer's real one. A test that writes to
~/.local/share/agentvoice would be a bug in the test, not a feature.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SANDBOX = Path(tempfile.mkdtemp(prefix="agentvoice-tests-"))

for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR"):
    d = _SANDBOX / var.lower()
    d.mkdir(parents=True, exist_ok=True)
    os.environ[var] = str(d)

sys.path.insert(0, str(ROOT / "daemon"))

import pytest  # noqa: E402


@pytest.fixture
def sandbox() -> Path:
    """The scratch root the XDG variables point into."""
    return _SANDBOX


@pytest.fixture
def cfg_factory():
    """Build a Config without touching shell.json or any file on disk.

    Config reads two files and merges them over the defaults. Tests care
    about the merged result, not the reading, so this bypasses the IO and
    hands back an object with the same interface.
    """
    from runtime import DEFAULTS, Config

    def make(**overrides):
        c = Config.__new__(Config)
        c._values = dict(DEFAULTS)
        c._values.update(overrides)
        c._stamps = ()
        c.source = "test"
        return c

    return make
