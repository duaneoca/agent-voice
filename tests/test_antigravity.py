"""The Antigravity envelope, a hung backend, and saying why one is missing.

Recorded from agy 1.2.8 answering live on 2026-09-23, not from --help. The
envelope is the whole contract here: one JSON object, emitted on failure as
well as success, from a process that exits 0 either way.
"""
from __future__ import annotations

import json
import subprocess
import time

import pytest

from adapters import explain
from adapters.antigravity import Antigravity
from adapters.base import Watchdog


def replay(lines, monkeypatch, **kw):
    class FakeProc:
        stdout = iter([ln + "\n" for ln in lines])
        stderr = None
        returncode = 0
        def wait(self): return 0
        def poll(self): return 0

    monkeypatch.setattr("adapters.antigravity.subprocess.Popen",
                        lambda argv, **k: FakeProc())
    return list(Antigravity(**kw).send("hi"))


SUCCESS = json.dumps({
    "conversation_id": "a653d848-5c83-42f9-b963-b895deaa76f4",
    "status": "SUCCESS",
    "response": "A wake word is a spoken phrase that activates a device.\n",
    "duration_seconds": 3.4, "num_turns": 1,
    "usage": {"input_tokens": 11810, "output_tokens": 27},
})
# Observed verbatim when unauthenticated -- note the process still exits 0.
FAILURE = json.dumps({
    "conversation_id": "", "status": "ERROR", "response": "",
    "error": "authentication failed or timed out",
    "duration_seconds": 0, "num_turns": 0, "usage": {},
})


class TestEnvelope:
    def test_the_answer_is_spoken(self, monkeypatch):
        text = "".join(c.text for c in replay([SUCCESS], monkeypatch) if c.text)
        assert "A wake word is a spoken phrase" in text

    def test_the_conversation_id_becomes_the_session(self, monkeypatch):
        chunks = replay([SUCCESS], monkeypatch)
        assert any(c.session_id == "a653d848-5c83-42f9-b963-b895deaa76f4"
                   for c in chunks)

    def test_failure_is_an_error_not_an_empty_answer(self, monkeypatch):
        """agy exits 0 when authentication fails. Trusting the exit code
        would turn that into a successful silent reply -- which in a voice
        interface is indistinguishable from the assistant ignoring you."""
        chunks = replay([FAILURE], monkeypatch)
        assert any(c.error == "authentication failed or timed out" for c in chunks)
        assert not any(c.text for c in chunks)

    def test_diagnostics_sharing_stdout_are_stepped_over(self, monkeypatch):
        noisy = ["Warning: something", "not json", "", SUCCESS]
        text = "".join(c.text for c in replay(noisy, monkeypatch) if c.text)
        assert "A wake word is a spoken phrase" in text

    def test_an_envelope_that_never_arrives_is_reported(self, monkeypatch):
        chunks = replay(["just chatter"], monkeypatch)
        assert any(c.error for c in chunks)


class TestLevelsBecomeFlags:
    """The level is declared as a flag before the turn rather than asked
    during it -- which is why a backend that cannot prompt can still have
    more than one honest posture."""

    @pytest.mark.parametrize("level,expected", [
        ("ask", ["--mode", "plan"]),
        ("trusted", ["--dangerously-skip-permissions"]),
    ])
    def test_each_level_maps_to_its_flag(self, level, expected):
        argv = Antigravity(level=level, cwd="/tmp")._argv("hi", None)
        for token in expected:
            assert token in argv
        if level != "trusted":
            assert "--dangerously-skip-permissions" not in argv

    def test_edits_is_not_offered(self):
        """Measured, not assumed: asked to write outside the project at
        --mode accept-edits --add-dir <project>, agy did it and reported
        SUCCESS -- with default settings and with allowNonWorkspaceAccess
        off. Our "edits" means confined to the project, so offering it here
        would import a guarantee from the Claude Code row of the same screen.
        """
        assert "edits" not in Antigravity().levels

    def test_a_stale_stored_edits_cannot_reach_the_flag(self):
        """Someone who chose edits before it was withdrawn must not keep
        getting accept-edits."""
        from adapters import load
        a = load("agy", level="edits", cwd="/tmp")
        assert a is None or a.level == "ask"
        argv = Antigravity(cwd="/tmp")
        argv.level = "edits"          # forced past every constructor guard
        assert "accept-edits" not in argv._argv("hi", None)

    def test_the_posture_does_not_claim_confinement(self):
        assert "not confined" in Antigravity().posture("trusted")

    def test_the_project_is_the_workspace(self):
        argv = Antigravity(cwd="/tmp/project")._argv("hi", None)
        assert argv[argv.index("--add-dir") + 1] == "/tmp/project"

    def test_a_session_resumes(self):
        argv = Antigravity()._argv("hi", "abc-123")
        assert argv[argv.index("--conversation") + 1] == "abc-123"


class TestWatchdog:
    def test_silence_is_killed(self):
        proc = subprocess.Popen(["sleep", "30"], stdout=subprocess.PIPE, text=True)
        dog = Watchdog(proc, 1.0)
        start = time.monotonic()
        for _ in proc.stdout:
            dog.poke()
        proc.wait(); dog.stop()
        assert dog.fired
        assert time.monotonic() - start < 10

    def test_a_backend_that_keeps_talking_is_left_alone(self):
        """Idle, not total: a long task that streams is still working."""
        proc = subprocess.Popen(
            ["bash", "-c", "for i in 1 2 3 4; do echo tick; sleep 0.5; done"],
            stdout=subprocess.PIPE, text=True)
        dog = Watchdog(proc, 1.2)
        for _ in proc.stdout:
            dog.poke()
        proc.wait(); dog.stop()
        assert not dog.fired


class TestExplain:
    def test_an_unknown_agent(self):
        assert "not an agent" in explain("nonsuch")

    def test_nothing_chosen(self, monkeypatch):
        """Must not depend on which agent this machine happens to have set."""
        import adapters
        monkeypatch.setattr(adapters, "omarchy_default", lambda: None)
        assert "omarchy default agent" in explain("")

    def test_a_healthy_agent_is_not_slandered(self, monkeypatch):
        """explain() is also used to check, so it must not invent a fault."""
        monkeypatch.setattr("adapters.antigravity.installed", lambda _: True)
        assert explain("agy") == ""
