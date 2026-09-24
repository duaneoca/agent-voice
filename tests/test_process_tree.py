"""Stopping a backend means stopping what it spawned.

Every agent CLI here is a shim that execs something else -- mise to node,
npm to a bundle -- and the grandchild inherits the stdout pipe. Killing the
named process leaves that pipe open, so the read loop waiting on it never
returns: a timeout that fires, reports a failure, and changes nothing.

Measured against gemini before the fix: parent dead, node alive, stdout
still open twenty seconds later. The earlier `sleep`-based test passed
because `sleep` has no children.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time

import pytest

from adapters.base import stop_tree


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class TestStopTree:
    def test_a_group_leader_and_its_children_all_go(self):
        proc = subprocess.Popen(
            ["bash", "-c", "sleep 60 & sleep 60"], start_new_session=True)
        time.sleep(0.5)
        kids = subprocess.run(["pgrep", "-P", str(proc.pid)],
                              capture_output=True, text=True).stdout.split()
        assert kids, "expected the shell to have spawned a child"
        stop_tree(proc)
        time.sleep(0.5)
        assert proc.poll() is not None
        assert not any(alive(int(k)) for k in kids), "a child outlived the kill"

    def test_it_refuses_to_kill_the_caller(self):
        """The bug this closes, which it caused before it was caught: for a
        child started without start_new_session, getpgid returns *our* group,
        so killpg takes down the caller and everything beside it. It killed
        the test run that was checking it.
        """
        proc = subprocess.Popen(["sleep", "30"])          # same group as us
        assert os.getpgid(proc.pid) == os.getpgid(0), "expected a non-leader"
        stop_tree(proc)
        time.sleep(0.4)
        assert proc.poll() is not None, "the child should still be stopped"
        assert alive(os.getpid()), "we killed ourselves"

    def test_an_already_dead_process_is_not_an_error(self):
        proc = subprocess.Popen(["true"])
        proc.wait()
        stop_tree(proc)          # must not raise

    def test_something_that_is_not_a_process_is_not_an_error(self):
        class NotAProcess:
            pid = None
        stop_tree(NotAProcess())

    @pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
    def test_both_signals_work(self, sig):
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        stop_tree(proc, sig)
        time.sleep(0.4)
        assert proc.poll() is not None


class TestAdaptersStartTheirOwnGroup:
    """stop_tree can only reach the tree if the child leads a group."""

    @pytest.mark.parametrize("module", [
        "gemini", "codex", "cli_agent", "claude_code", "antigravity"])
    def test_every_subprocess_adapter_asks_for_one(self, module):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "daemon" / "adapters" / f"{module}.py").read_text()
        assert "start_new_session=True" in src, (
            f"{module} spawns without its own session, so a timeout or an "
            f"interrupt would leave its children holding the pipe")
