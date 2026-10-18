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


ROOT = Path(__file__).resolve().parent.parent
DAEMON_SRC = (ROOT / "daemon" / "wake_listen.py").read_text()


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


class TestALockReachesAReplyInProgress:
    """The loop polled the lock at its top -- the one place the daemon is not
    when it matters. `answer()` blocks for the whole agent turn and the whole
    of speech playback, 22 to 45 seconds in this machine's journal, so locking
    the screen mid-reply left it reading a private answer aloud to an empty
    room until the reply ended. Reported against 1d59790 by a reviewer.
    """

    def test_the_reply_loop_polls_the_session(self):
        src = (ROOT / "daemon" / "wake_listen.py").read_text()
        body = src[src.index("    def answer("):]
        body = body[:body.index("\n    def ")]
        assert "self.poll_session()" in body, \
            "a reply cannot notice a lock that only the main loop looks for"
        loop = body[body.index("for sentence in sentences("):]
        assert loop.index("self.poll_session()") < loop.index("self.speak("), \
            "it has to look before it speaks the next sentence, not after"

    def test_a_lock_stops_the_reply(self, daemon, monkeypatch):
        """Driven: the speaker is told to stop and the remaining sentences are
        never said."""
        from adapters.base import Chunk

        said = []

        class Agent:
            def send(self, text, session_id=None):
                for n in range(6):
                    yield Chunk(text=f"Sentence number {n}. ")

            def cancel(self):
                pass

        def speak(sentence, tail_ms=0):
            said.append(sentence)
            if len(said) == 2:               # the screen locks mid-reply
                daemon.locked = True

        daemon.agent = Agent()
        monkeypatch.setattr(daemon, "speak", speak)
        monkeypatch.setattr(daemon, "poll_session", lambda: None)
        daemon.answer("say six things", {"transcript": "say six things"})

        assert len(said) == 2, f"it kept speaking after the lock: {said}"
        assert daemon.speaker.cancelled, "silence() was never called"

    def test_one_implementation_of_an_explicit_lock(self):
        """The loop had its own copy. Two copies of "what a lock means" is how
        they drift -- and the reply path needs the same one."""
        src = (ROOT / "daemon" / "wake_listen.py").read_text()
        assert src.count("_lock_hold_until = time.time() + LOCK_HOLD_S") == 1
        assert "def apply_lock_command" in src
        assert "if self.apply_lock_command(cmd):" in src


class TestALockReachesTheSentenceBeingSpoken:
    """The second half of the same finding, reported against 98697f5.

    Polling between sentences moved the exposure from the whole reply down to
    one sentence; it did not close it. `speak()` is synchronous for as long as
    the audio lasts, and `_barge_watch()` returned None whenever `bargeIn` was
    off -- which is the default, and measured: where the microphone sits
    beside the speaker, a gate that hears the user fires on our own output
    too. So nothing could stop a sentence once it had started, and a lock two
    words into a long one read the rest of it out to a locked room.
    """

    def _speaker(self, daemon, lock_after=None):
        played = []

        class Speaker:
            name = "stub"

            def __init__(self):
                self.cancelled = 0

            def say(self, text, watch=None):
                for i in range(10):
                    if watch is not None and watch():
                        return
                    played.append(i)
                    if lock_after is not None and i == lock_after:
                        daemon.locked = True        # the screen locks mid-sentence

            def cancel(self):
                self.cancelled += 1

        daemon.speaker = Speaker()
        daemon.enabled, daemon.locked = True, False
        return played

    def test_a_lock_stops_the_audio_mid_sentence(self, daemon, monkeypatch):
        monkeypatch.setattr(daemon, "poll_session", lambda: None)
        played = self._speaker(daemon, lock_after=2)

        daemon.speak("a sentence long enough to matter", 0)

        assert len(played) == 3, \
            f"the rest of the sentence was read to a locked room: {played}"

    def test_the_watch_is_there_with_barge_in_off(self, daemon, monkeypatch):
        """The regression precisely: barge-in was the only reason to look
        between chunks, so turning it off removed the only means of stopping."""
        monkeypatch.setattr(daemon, "poll_session", lambda: None)
        daemon.cfg._values["bargeIn"] = False
        assert daemon._barge_watch() is None, "barge-in is off for this test"

        watch = daemon._watch(None)
        assert callable(watch), "nothing could interrupt a sentence"
        daemon.enabled, daemon.locked = True, False
        assert watch() is False, "it must not stop an unlocked session"
        daemon.locked = True
        assert watch() is True, "a locked session has to stop the audio"

    def test_the_echo_path_is_guarded_by_the_same_function(self, daemon,
                                                           monkeypatch):
        """With no agent configured the reply is the transcript read back, and
        it reaches the speaker without passing the between-sentence poll at
        all. The guard is in `speak()` for that reason: one function every
        path that makes a noise goes through."""
        monkeypatch.setattr(daemon, "poll_session", lambda: None)
        played = self._speaker(daemon)
        daemon.agent = None
        daemon.locked = True                        # already locked

        daemon.answer("say this back to me", {"transcript": "x"})

        assert played == [], f"it spoke into a locked room: {played}"
        assert daemon.speaker.cancelled, "silence() was never called"

    def test_the_lock_is_polled_while_a_sentence_plays(self, daemon,
                                                       monkeypatch):
        """The compositor check is a 9ms process spawn, so it runs on a thread
        rather than between audio chunks -- but it does have to run, or only
        `agentvoice lock` could reach a sentence and a real screen lock could
        not."""
        import time as _time

        polls = []
        monkeypatch.setattr(daemon, "poll_session", lambda: polls.append(1))

        class Slow:
            name = "stub"
            cancelled = 0

            def say(self, text, watch=None):
                _time.sleep(0.8)                    # a sentence of audio

            def cancel(self):
                pass

        daemon.speaker = Slow()
        daemon.enabled, daemon.locked = True, False
        daemon.speak("a long sentence", 0)

        # One before the first chunk, then the thread every 250ms.
        assert len(polls) >= 3, \
            f"nothing looked for a lock while the audio played: {len(polls)}"


class TestALockReachesTheAgentsOwnTurn:
    """The third place a lock had to reach, after the reply loop and the
    sentence: the agent's turn before any sentence exists.

    `answer()` blocks the run loop for the whole turn -- 31 seconds in this
    machine's journal, of which the first six were before a word was spoken.
    Speech was safe once the between-sentence check existed, because nothing
    reached the speaker. The agent was not: it went on running whatever tools
    the permission level allowed, behind a locked screen, for as long as the
    thinking phase lasted.
    """

    def test_a_lock_while_thinking_cancels_the_agent(self, daemon, monkeypatch):
        import time as _time

        from adapters.base import Chunk

        class Agent:
            """A CLI that thinks for a while before its first word, and dies
            when cancelled -- which is what a cancel has to achieve here. The
            flag records *when* it was cancelled, because a cancel that only
            lands after the first sentence arrives is the between-sentence
            check doing the work and proves nothing about this."""

            name = "stub"

            def __init__(self):
                self.cancelled = 0
                self.cancelled_while_thinking = False

            def send(self, text, session_id=None):
                for _ in range(40):              # 2s of thinking, in steps
                    _time.sleep(0.05)
                    if self.cancelled:
                        self.cancelled_while_thinking = True
                        return                   # the CLI is gone
                yield Chunk(text="Something private. ")

            def cancel(self):
                self.cancelled += 1

        agent = Agent()
        daemon.agent = agent
        daemon.enabled, daemon.locked = True, False

        # Stubbed at the compositor, not at poll_session: the real chain is
        # watcher -> poll_session -> reconcile_lock -> silence -> agent.cancel,
        # and a fake that merely sets `locked` would skip the three steps in
        # the middle. The first version of this test did that and passed with
        # the watcher removed, which is how it came to be written this way.
        import wake_listen

        t0 = _time.perf_counter()
        monkeypatch.setattr(wake_listen, "session_locked",
                            lambda: _time.perf_counter() - t0 > 0.25)
        daemon._last_session_poll = 0.0          # no rate limit on the first look
        daemon.answer("something private", {"transcript": "x"})

        assert agent.cancelled_while_thinking, \
            "the agent kept working behind a locked screen for the whole turn"
        assert not daemon.speaker.said, "it spoke into a locked room"

    def test_the_watcher_does_not_start_a_thread_per_nesting(self, daemon):
        """`speak()` enters it too, for the echo path. Two threads polling the
        compositor twice as often is waste, and leaking one per sentence is
        worse than waste."""
        import threading

        before = threading.active_count()
        with daemon._watching_session():
            inside_one = threading.active_count()
            with daemon._watching_session():
                inside_two = threading.active_count()
        after = threading.active_count()

        assert inside_one == before + 1, "no watcher was started"
        assert inside_two == inside_one, "nesting started a second watcher"
        assert daemon._watchers == 0, "the count did not unwind"
        assert after == before, "the watcher thread was leaked"


class TestStopMeansStopListeningToo:
    """Reported from use: "when I hit stop on the panel, it stopped the
    current task, then immediately went back to listening for the next turn.
    I want it to stop listening for the next turn and go back to listening
    for the wake word."

    The follow-up window uses neither the wake word nor the speaker verifier,
    so leaving it armed after a stop means the next thing said in the room
    starts a turn -- and what somebody says just after reaching for a stop
    button is the most likely thing in the world to be about what just went
    wrong, said to somebody else.
    """

    def test_an_interrupt_closes_the_follow_up_window(self, daemon):
        daemon.pipe = type("P", (), {"conversation": True})()

        assert daemon.arm_follow_up() is True, "conversation mode is on"
        daemon._interrupt.set()
        assert daemon.arm_follow_up() is False, \
            "a stop left it listening for the next sentence"

    def test_a_lock_a_mute_and_a_yield_close_it_as_well(self, daemon):
        """All three go through `silence()`, which sets the same flag -- and
        all three mean "stop paying attention" just as much as the button."""
        daemon.pipe = type("P", (), {"conversation": True})()
        daemon.speaker = None
        daemon.agent = None

        daemon.silence()
        assert daemon.arm_follow_up() is False

    def test_conversation_mode_off_is_still_off(self, daemon):
        daemon.pipe = type("P", (), {"conversation": False})()
        assert daemon.arm_follow_up() is False

    def test_the_run_loop_asks_rather_than_deciding_for_itself(self):
        """So that a reason added to `arm_follow_up` reaches the loop, and the
        loop cannot grow a second opinion about it."""
        src = (ROOT / "daemon" / "wake_listen.py").read_text()
        body = src[src.index("    def run(self, device"):]
        assert "self.arm_follow_up()" in body
        assert "follow_up = self.pipe.conversation" not in body, \
            "the loop decided this inline again"
