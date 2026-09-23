"""Stopping a reply that is already underway.

Barge-in is not available on this hardware -- a microphone beside a speaker
hears 8/20 of its wake words against our own playback -- so the interrupt is
an explicit signal rather than something the detector notices. That makes the
rules worth pinning down: it must stop the *whole* remaining reply rather than
one sentence, it must not announce the cancelled agent as a failure, and it
must do nothing at all when the daemon is idle.
"""
from __future__ import annotations

import signal

import pytest

from adapters.base import Chunk
from runtime import StateFile
from wake_listen import Daemon


class FakeSpeaker:
    """Records what was said, and whether it was told to stop."""

    def __init__(self):
        self.name = "test"
        self.said: list[str] = []
        self.cancelled = False

    def say(self, text: str) -> float:
        self.said.append(text)
        return 0.0

    def cancel(self) -> None:
        self.cancelled = True


class FakeAgent:
    """Streams sentences, optionally interrupting partway through."""

    def __init__(self, texts, on_chunk=None, fail_after=None):
        self.texts = texts
        self.on_chunk = on_chunk
        self.fail_after = fail_after
        self.cancelled = False

    def send(self, text, session_id=None):
        for i, t in enumerate(self.texts):
            if self.fail_after is not None and i == self.fail_after:
                raise RuntimeError("agent died")
            yield Chunk(text=t, session_id="s1")
            if self.on_chunk:
                self.on_chunk(i)

    def cancel(self) -> None:
        self.cancelled = True


@pytest.fixture
def daemon(cfg_factory):
    """A Daemon with fake audio and no agent, ready to have one attached."""
    saved = (signal.getsignal(signal.SIGUSR1), signal.getsignal(signal.SIGUSR2))
    cfg = cfg_factory(echoTailMs=0, speakReplies=True)
    d = Daemon(cfg, pipe=None, state=StateFile(), speaker=FakeSpeaker(), agent=None)
    yield d
    signal.signal(signal.SIGUSR1, saved[0])
    signal.signal(signal.SIGUSR2, saved[1])


def test_idle_interrupt_is_a_no_op(daemon):
    """A stray keypress must not disturb a daemon that is only listening."""
    daemon.state.publish("listening")
    daemon._on_interrupt()
    assert not daemon._interrupt.is_set()
    assert not daemon.speaker.cancelled


@pytest.mark.parametrize("state", ["thinking", "speaking"])
def test_interrupt_mid_turn_cancels_both_ends(daemon, state):
    """Speaking and thinking are both mid-turn; both must stop."""
    daemon.agent = FakeAgent([])
    daemon.state.publish(state)
    daemon._on_interrupt()
    assert daemon._interrupt.is_set()
    assert daemon.speaker.cancelled
    assert daemon.agent.cancelled


# Long enough that iter_sentences emits one per chunk: it merges fragments
# too short to be worth speaking, so "One. Two." would arrive as a single
# utterance and the test would prove nothing.
REPLY = [
    "The retry loop spins forever when the socket fails. ",
    "The token refresh path repeats that same mistake. ",
    "The pagination cursor never advances past the first page. ",
]


def test_interrupt_stops_the_rest_of_the_reply(daemon):
    """The bug worth guarding: cancelling one sentence, then speaking the next.

    Speaker.cancel() only ends the utterance in flight, so without a flag the
    loop would cheerfully start the sentence after it.
    """
    daemon.agent = FakeAgent(
        REPLY, on_chunk=lambda i: daemon._on_interrupt() if i == 0 else None
    )
    daemon.state.publish("speaking")
    daemon.answer("hello", {})
    assert daemon.speaker.said == [REPLY[0].strip()]


def test_interrupt_does_not_announce_a_failure(daemon):
    """Killing the agent surfaces as an error. Saying so talks over the user."""
    # The interrupt arrives mid-stream, the way a keypress would; answer()
    # clears the flag on entry, so setting it beforehand would prove nothing.
    daemon.agent = FakeAgent(
        REPLY, fail_after=2,
        on_chunk=lambda i: daemon._on_interrupt() if i == 0 else None,
    )
    daemon.state.publish("speaking")
    daemon.answer("hello", {})
    assert "Sorry, the agent failed." not in daemon.speaker.said


def test_a_real_failure_is_still_announced(daemon):
    """The suppression above must not swallow genuine breakage."""
    daemon.agent = FakeAgent(REPLY, fail_after=0)
    daemon.state.publish("thinking")
    daemon.answer("hello", {})
    assert "Sorry, the agent failed." in daemon.speaker.said


def test_a_new_turn_clears_a_stale_interrupt(daemon):
    """Otherwise one interrupt would mute every reply that followed it."""
    daemon.agent = FakeAgent(REPLY[:1])
    daemon._interrupt.set()
    daemon.state.publish("thinking")
    daemon.answer("hello", {})
    assert daemon.speaker.said == [REPLY[0].strip()]


def test_transcript_records_only_what_was_spoken(daemon):
    """The panel shows the last exchange; it should not show unsaid text.

    The agent keeps generating for a moment after it is told to stop, so a
    sentence can arrive between the interrupt and the break.
    """
    daemon.agent = FakeAgent(
        REPLY, on_chunk=lambda i: daemon._on_interrupt() if i == 0 else None
    )
    daemon.state.publish("speaking")
    last = {}
    daemon.answer("hello", last)
    assert last["reply"] == REPLY[0].strip()


class TestCommandChannel:
    """Push-to-talk arrives as a file, because a signal cannot carry two edges.

    The run loop reads this every frame, so a malformed or half-written file
    has to be survivable: returning None and leaving the loop alone is always
    better than raising inside the audio path.
    """

    def test_absent_command_is_none(self, daemon):
        assert daemon.take_command() is None

    def test_command_is_read_once(self, daemon):
        from runtime import RUNTIME_DIR
        (RUNTIME_DIR / "command").write_text("talk")
        assert daemon.take_command() == "talk"
        # Consumed: a command left in place would re-fire every 100ms.
        assert daemon.take_command() is None

    def test_whitespace_is_tolerated(self, daemon):
        from runtime import RUNTIME_DIR
        (RUNTIME_DIR / "command").write_text("  talk-end \n")
        assert daemon.take_command() == "talk-end"

    def test_a_directory_in_the_way_does_not_raise(self, daemon):
        """Anything unreadable must be swallowed, not thrown into the loop."""
        from runtime import RUNTIME_DIR
        d = RUNTIME_DIR / "command"
        if d.exists():
            d.unlink()
        d.mkdir()
        try:
            assert daemon.take_command() is None
        finally:
            d.rmdir()
