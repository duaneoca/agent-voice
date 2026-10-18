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

# paths.SHELL_JSON is computed at import time, so redirecting the XDG
# variables above only works for modules imported after it -- runtime has
# already captured the value. Rebound here, because without it the suite
# reads the developer's own settings: the permission tests then pass or fail
# according to whatever level happens to be set on the machine, which is how
# a `decide()` that answered "allow" to `rm -rf /` got a green run.
import runtime  # noqa: E402

runtime.SHELL_JSON = _SANDBOX / "shell.json"

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


# --- a Daemon with its moving parts replaced --------------------------------
# Lived in test_lock.py until a second suite needed the same thing. Kept here
# rather than imported across test modules, which pytest tolerates and nobody
# can follow.

class FakeSpeaker:
    """Stands in for Piper, including the part that matters for a lock.

    `say()` consults `watch` between audio chunks, the way the real one does
    between the buffers it writes to PortAudio -- so a test can lock the
    session partway through a sentence and see where the audio stopped. The
    first version of this recorded the text and ignored `watch` entirely,
    which is why a reviewer found the gap rather than the suite: with the
    watch never called, a watch that was never passed looked the same.

    `chunks` is how many pieces a sentence is delivered in, and `played`
    counts the ones that actually went out.
    """

    chunks = 10

    def __init__(self):
        self.said = []
        self.cancelled = 0
        self.played = 0
        self.stopped_early = False

    def say(self, text, watch=None):
        self.said.append(text)
        for _ in range(self.chunks):
            if watch is not None and watch():
                self.stopped_early = True
                return
            self.played += 1

    def cancel(self):
        self.cancelled += 1


class FakeAgent:
    def __init__(self):
        self.cancelled = 0

    def send(self, text, session_id=None):
        return iter(())

    def cancel(self):
        self.cancelled += 1


@pytest.fixture
def daemon(cfg_factory):
    """Signals are process-global, and Daemon installs two in its constructor,
    so they are put back afterwards or a later test inherits them."""
    import signal

    from runtime import StateFile
    from wake_listen import Daemon

    saved = (signal.getsignal(signal.SIGUSR1), signal.getsignal(signal.SIGUSR2))
    d = Daemon(cfg_factory(echoTailMs=0), pipe=None, state=StateFile(),
               speaker=FakeSpeaker(), agent=FakeAgent())
    yield d
    signal.signal(signal.SIGUSR1, saved[0])
    signal.signal(signal.SIGUSR2, saved[1])
