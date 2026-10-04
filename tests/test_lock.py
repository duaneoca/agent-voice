"""The microphone closes while the screen is locked.

The hole this closes: the daemon kept listening behind a lock screen, so
anyone within earshot of a locked machine could hold a conversation with an
agent that had been granted "edits" or "trusted". Every permission decision
in this project, undone by walking up to the desk.

Two flags rather than one is the load-bearing part. `enabled` is what the
user asked for -- the panel switch, the panic keybind -- and `locked` is the
session's state. Nothing is saved and restored around a lock, so there is no
stored copy to fall out of step, and unlocking cannot turn the microphone
*on* for someone who had deliberately turned it off.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from runtime import StateFile
from wake_listen import Daemon

ROOT = Path(__file__).resolve().parent.parent
DAEMON_SRC = (ROOT / "daemon" / "wake_listen.py").read_text()


class FakeSpeaker:
    def __init__(self):
        self.name = "test"
        self.cancelled = False

    def say(self, text, watch=None):
        return 0.0

    def cancel(self):
        self.cancelled = True


class FakeAgent:
    def __init__(self):
        self.cancelled = False

    def send(self, text, session_id=None):
        return iter(())

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def daemon(cfg_factory):
    saved = (signal.getsignal(signal.SIGUSR1), signal.getsignal(signal.SIGUSR2))
    d = Daemon(cfg_factory(echoTailMs=0), pipe=None, state=StateFile(),
               speaker=FakeSpeaker(), agent=FakeAgent())
    yield d
    signal.signal(signal.SIGUSR1, saved[0])
    signal.signal(signal.SIGUSR2, saved[1])


def _stub_detector(tmp_path, monkeypatch, exit_code: int):
    """Put a fake omarchy-hyprland-session-locked on PATH."""
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    stub = bin_ / "omarchy-hyprland-session-locked"
    stub.write_text(f"#!/bin/bash\nexit {exit_code}\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    return stub


class TestDetection:
    """Omarchy's own detector, read rather than reimplemented."""

    @pytest.mark.parametrize("code,expected", [(0, True), (1, False)])
    def test_the_exit_code_is_the_answer(self, tmp_path, monkeypatch,
                                         code, expected):
        import wake_listen
        _stub_detector(tmp_path, monkeypatch, code)
        assert wake_listen.session_locked() is expected

    def test_undetermined_is_neither(self, tmp_path, monkeypatch):
        """Exit 2 means Hyprland was not able to say. False would be the
        fail-open answer; True would shut the microphone for good on a
        machine with no compositor to ask."""
        import wake_listen
        _stub_detector(tmp_path, monkeypatch, 2)
        monkeypatch.delenv("XDG_SESSION_ID", raising=False)
        assert wake_listen.session_locked() is None

    def test_a_missing_detector_is_not_a_lock(self, tmp_path, monkeypatch):
        """A development run in a terminal has no Omarchy on PATH, and must
        not end up with a microphone that never opens."""
        import wake_listen
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.delenv("XDG_SESSION_ID", raising=False)
        assert wake_listen.session_locked() is None


class TestTheGate:
    def test_listening_needs_both(self, daemon):
        for enabled in (True, False):
            for locked in (True, False):
                daemon.enabled, daemon.locked = enabled, locked
                assert daemon.listening is (enabled and not locked)

    def test_a_lock_does_not_touch_what_the_user_asked_for(self, daemon,
                                                           tmp_path, monkeypatch):
        """The whole reason there are two flags. Toggling `enabled` on lock
        would have turned the microphone *on* for anyone who had turned it
        off, and "restoring" it afterwards needs a stored copy that can go
        stale."""
        daemon.enabled = False
        _stub_detector(tmp_path, monkeypatch, 0)
        daemon.reconcile_lock()
        assert daemon.locked is True
        assert daemon.enabled is False, "the lock rewrote the user's choice"

        _stub_detector(tmp_path, monkeypatch, 1)
        daemon.reconcile_lock()
        assert daemon.locked is False
        assert daemon.enabled is False, "unlocking turned the microphone on"
        assert daemon.listening is False

    def test_unlocking_reveals_the_intent_that_was_there(self, daemon,
                                                         tmp_path, monkeypatch):
        daemon.enabled = True
        _stub_detector(tmp_path, monkeypatch, 0)
        daemon.reconcile_lock()
        assert daemon.listening is False
        _stub_detector(tmp_path, monkeypatch, 1)
        daemon.reconcile_lock()
        assert daemon.listening is True


class TestLockingStopsWhatIsUnderway:
    def test_it_stops_a_reply_being_read_aloud(self, daemon, tmp_path,
                                               monkeypatch):
        """A lock mid-reply is someone walking away while the machine reads
        their mail to the room."""
        _stub_detector(tmp_path, monkeypatch, 0)
        daemon.reconcile_lock()
        assert daemon.speaker.cancelled
        assert daemon.agent.cancelled
        assert daemon._interrupt.is_set()

    def test_the_interrupt_is_cleared_again_on_unlock(self, daemon, tmp_path,
                                                     monkeypatch):
        """Otherwise the first turn after unlocking is cancelled before it
        starts, which looks exactly like a broken wake word."""
        _stub_detector(tmp_path, monkeypatch, 0)
        daemon.reconcile_lock()
        _stub_detector(tmp_path, monkeypatch, 1)
        daemon.reconcile_lock()
        assert not daemon._interrupt.is_set()

    def test_captured_audio_is_dropped_rather_than_queued(self, daemon):
        """Frames pile up in the queue while the loop is busy. Without a
        drain, the audio recorded in the first moments of a lock would be
        sitting there waiting to be transcribed on unlock."""
        import queue
        daemon._frames = queue.Queue()
        for _ in range(5):
            daemon._frames.put(b"\x00\x00" * 160)
        daemon.silence()
        assert daemon._frames.empty()


class TestTheGraceAfterAnAnnouncedLock:
    """`agentvoice lock` has to outrank the poll for a moment.

    `solitaryBlockedBy` does not gain LOCK the instant the lock key is
    pressed. Without a grace period, binding the command made things *worse*
    than not binding it: the poll reopened the microphone for the couple of
    seconds right after locking -- precisely the window the binding closes.
    """

    def test_the_poll_does_not_reopen_during_the_grace(self, daemon, tmp_path,
                                                       monkeypatch):
        import time

        import wake_listen
        daemon.locked = True
        daemon._lock_hold_until = time.time() + wake_listen.LOCK_HOLD_S
        _stub_detector(tmp_path, monkeypatch, 1)       # says unlocked
        daemon.reconcile_lock()
        assert daemon.locked is True, "the poll overrode a fresh announcement"

    def test_the_poll_wins_again_once_the_grace_expires(self, daemon, tmp_path,
                                                        monkeypatch):
        """A stale announcement must not hold the microphone shut."""
        daemon.locked = True
        daemon._lock_hold_until = 0.0
        _stub_detector(tmp_path, monkeypatch, 1)
        daemon.reconcile_lock()
        assert daemon.locked is False

    def test_a_real_lock_is_never_held_open(self, daemon, tmp_path, monkeypatch):
        """The grace only ever delays *unlocking*. Locking is immediate."""
        import time
        daemon.locked = False
        daemon._lock_hold_until = time.time() + 999
        _stub_detector(tmp_path, monkeypatch, 0)
        daemon.reconcile_lock()
        assert daemon.locked is True


class TestItIsVisible:
    def test_locked_is_its_own_published_state(self, daemon, tmp_path,
                                               monkeypatch):
        """Not "off": the panel has to be able to say *why* the microphone is
        closed, because "off" invites someone to switch it back on and
        wonder why nothing happens."""
        import json
        _stub_detector(tmp_path, monkeypatch, 0)
        daemon.reconcile_lock()
        assert json.loads(daemon.state.path.read_text())["state"] == "locked"

    def test_the_panel_draws_that_state(self):
        """A state the daemon publishes and the panel has no case for shows
        as "STARTING" forever."""
        panel = (ROOT / "Panel.qml").read_text()
        assert '"locked"' in panel, "Panel.qml has no case for the locked state"
        label = panel[panel.index("readonly property string stateLabel:"):]
        label = label[:label.index("\n  }")]
        assert '"locked"' in label


class TestTheGateIsWhereItMatters:
    """Source checks, because the alternative is a microphone test.

    CLAUDE.md says not to test the audio loop, and these are the two lines in
    it that decide whether a locked machine listens.
    """

    def test_the_frame_loop_gates_on_listening_not_enabled(self):
        loop = DAEMON_SRC[DAEMON_SRC.index("    def run(self, device"):]
        assert "if not self.listening:" in loop, \
            "the per-frame gate must consider the lock, not only the toggle"
        assert "if not self.enabled:\n                    continue" not in loop

    def test_the_lock_is_polled_above_the_gate_not_below_it(self):
        """The bug this pins: reconcile_lock() lived in refresh(), which the
        loop reaches *after* the gate. So the microphone closed on lock and
        then nothing ever looked again -- it stayed closed after unlocking
        until the service was restarted, which is the exact failure the
        feature exists to prevent. Caught by locking the running daemon and
        watching it never come back.
        """
        loop = DAEMON_SRC[DAEMON_SRC.index("    def run(self, device"):]
        polled = loop.index("self.reconcile_lock()")
        gate = loop.index("if not self.listening:")
        assert polled < gate, \
            "the lock poll is below the gate, so it stops running once it fires"

        refresh = DAEMON_SRC[DAEMON_SRC.index("    def refresh(self):"):]
        refresh = refresh[:refresh.index("\n    def ", 10)]
        assert "self.reconcile_lock()" not in refresh, \
            "refresh() is called from below the gate; it must not own this"

    def test_the_lock_poll_has_its_own_clock(self):
        """Sharing `last_poll` would tie it to the config reload, which is
        also below the gate."""
        loop = DAEMON_SRC[DAEMON_SRC.index("    def run(self, device"):]
        assert "last_lock_poll" in loop

    def test_push_to_talk_is_gated_too(self):
        """It bypasses the wake word, so a lock that only stopped the wake
        word would leave the keybind working on a locked machine."""
        assert 'cmd == "talk" and self.listening' in DAEMON_SRC

    def test_the_follow_up_window_is_gated_too(self):
        assert "if follow_up and self.listening:" in DAEMON_SRC

    def test_a_capture_underway_is_abandoned_rather_than_suspended(self):
        """Frames stop arriving, but `buf` keeps whatever was recorded up to
        the lock -- so the utterance would resume on unlock and be
        transcribed joined to whatever was said next."""
        loop = DAEMON_SRC[DAEMON_SRC.index("    def run(self, device"):]
        gate = loop[loop.index("if not self.listening:"):]
        gate = gate[:gate.index("continue") + 8]
        assert 'phase, buf, cap' in gate, "the capture is left half-finished"
        assert '"wake"' in gate
        assert "_empty_turn()" in gate, "the readout keeps the old transcript"

    def test_not_being_able_to_tell_is_said_out_loud(self, daemon, tmp_path,
                                                     monkeypatch, capsys):
        """A fail-open that nobody is told about is the worst of the three
        outcomes: the microphone stays open behind a lock and the only
        evidence is its absence."""
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.delenv("XDG_SESSION_ID", raising=False)
        daemon.reconcile_lock()
        said = capsys.readouterr().out
        assert "cannot tell whether the screen is locked" in said
        assert "will not close on lock" in said
        assert daemon.locked is False, "it must not guess either way"

    def test_it_says_so_once_rather_than_every_poll(self, daemon, tmp_path,
                                                    monkeypatch, capsys):
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.delenv("XDG_SESSION_ID", raising=False)
        for _ in range(5):
            daemon.reconcile_lock()
        assert capsys.readouterr().out.count("cannot tell") == 1

    def test_the_cli_can_say_it_without_waiting_for_the_poll(self):
        """For binding to whatever locks the screen. The poll is still the
        guarantee -- it needs nothing installed -- so this is an
        optimisation, and it must not fail when voice is off."""
        cli = (ROOT / "bin" / "agentvoice").read_text()
        assert "lock|unlock)" in cli
        block = cli[cli.index("lock|unlock)"):]
        block = block[:block.index(";;")]
        assert "|| true" in block, "a lock hook must not report a failure"

    def test_asking_whether_it_is_locked_answers_in_the_exit_status(self):
        """`[[ x == locked ]] && echo yes || echo no` exits 0 either way, so
        the obvious `if agentvoice locked; then` read as locked always --- a
        script guarding anything on it did nothing, in the unsafe direction.

        The shipped lines are run rather than grepped, because what is being
        asserted is an exit status and the two paths are one `||` away from
        being wrong again. They are lifted out of the script instead of
        invoking it, so that no daemon and no systemd session are needed:
        `vstate` consults both.
        """
        cli = (ROOT / "bin" / "agentvoice").read_text()
        block = cli[cli.index("  locked)") + len("  locked)"):]
        block = block[:block.index(";;")]
        assert "echo yes" in block and "echo no" in block, block

        for state, expect_yes in (("locked", True), ("listening", False)):
            proc = subprocess.run(
                ["bash", "-c", f"vstate() {{ echo {state}; }}\n{block}"],
                capture_output=True, text=True)
            assert (proc.stdout.strip() == "yes") is expect_yes, proc.stdout
            assert (proc.returncode == 0) is expect_yes, \
                f"{state!r} exited {proc.returncode}: the status is the answer"
