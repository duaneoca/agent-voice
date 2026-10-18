"""How quickly the speaker can be made to stop.

Every way of stopping speech in this project ends at the same two
mechanisms: `Speaker.cancel()`, which sets a flag, and the `watch` callable,
which is consulted while audio plays. A locked screen, a mute, another
application taking the microphone, the Stop button, the interrupt keybind and
barge-in are all one of those two.

They were all ineffective, for a reason no amount of reading the daemon would
have shown: Piper returns an entire sentence as a single chunk -- 10.53
seconds of audio in one 464KB buffer, measured on lessac-medium -- and
`out.write()` of that blocks until nearly all of it has played. `_stop` was
checked only before a chunk and `watch` only after one. Measured against the
installed build: cancel() one second in, and it kept talking for 10.29
seconds. After slicing, 0.03 seconds.

Driven with a fake PortAudio and a fake numpy rather than skipped for want of
either, because the bare CI runner is where this most needs to keep working.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class FakeStream:
    """What sounddevice.OutputStream gives back, as far as say() uses it."""

    def __init__(self, **kw):
        self.writes: list[int] = []
        self.aborted = 0
        self.closed = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed += 1
        return False

    def write(self, pcm):
        self.writes.append(len(pcm))

    def abort(self):
        self.aborted += 1


class FakeVoice:
    """Piper, with its real chunking: one sentence, one chunk."""

    def __init__(self, seconds=10.0, rate=22050):
        self.config = types.SimpleNamespace(sample_rate=rate)
        self._bytes = b"\x00\x01" * int(seconds * rate)

    def synthesize(self, sentence):
        yield types.SimpleNamespace(audio_int16_bytes=self._bytes)


def _speaker(monkeypatch, seconds=10.0):
    """A Speaker with no model and no audio device."""
    import runtime

    stream = FakeStream()
    fake_sd = types.ModuleType("sounddevice")
    fake_sd.OutputStream = lambda **kw: stream
    fake_np = types.ModuleType("numpy")
    fake_np.int16 = "int16"
    # A real memoryview: it has len() and slices, which is all say() asks of it.
    fake_np.frombuffer = lambda b, dtype=None: memoryview(b).cast("h")
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    monkeypatch.setitem(sys.modules, "numpy", fake_np)

    sp = runtime.Speaker.__new__(runtime.Speaker)
    import threading

    sp._voice = FakeVoice(seconds)
    sp._rate = 22050
    sp._stop = threading.Event()
    sp._name = "fake"
    sp._wanted = "fake"
    return sp, stream


def test_a_sentence_is_written_in_slices_not_in_one_blocking_call(monkeypatch):
    """The root cause. One write of a whole sentence cannot be interrupted by
    anything, so no flag and no callback could ever have worked."""
    import runtime

    sp, stream = _speaker(monkeypatch, seconds=2.0)
    sp.say("whatever")

    assert len(stream.writes) > 1, \
        "the sentence went out in one write; nothing can interrupt that"
    biggest_ms = max(stream.writes) / 22050 * 1000
    assert biggest_ms <= runtime.SLICE_MS + 1, \
        f"a single write covers {biggest_ms:.0f}ms of audio, so a stop waits that long"


def test_cancel_stops_within_one_slice(monkeypatch):
    """`cancel()` is what a lock, a mute, a yield and the Stop button all
    reach. Measured in slices rather than seconds, since there is no clock
    here -- one slice is 50ms of audio."""
    import runtime

    sp, stream = _speaker(monkeypatch, seconds=10.0)

    # Cancel after the third slice has been handed over, the way another
    # thread would partway through a sentence.
    real_write = stream.write

    def write(pcm):
        real_write(pcm)
        if len(stream.writes) == 3:
            sp.cancel()

    stream.write = write
    produced = sp.say("a sentence long enough to lock during")

    assert len(stream.writes) == 3, \
        f"it wrote {len(stream.writes)} slices after being cancelled at 3"
    assert produced <= 4 * runtime.SLICE_MS / 1000, \
        f"it claims {produced:.2f}s of audio from a sentence it abandoned"


def test_what_is_already_buffered_is_dropped(monkeypatch):
    """Stopping the writes is not stopping the sound: whatever is inside
    PortAudio's buffer goes on playing. stop() and close() drain it, which is
    the wrong direction for every caller that asked for silence."""
    sp, stream = _speaker(monkeypatch, seconds=10.0)

    real_write = stream.write

    def write(pcm):
        real_write(pcm)
        if len(stream.writes) == 2:
            sp.cancel()

    stream.write = write
    sp.say("a sentence")

    assert stream.aborted == 1, "the buffered audio was left to play out"


def test_a_sentence_that_finishes_is_not_aborted(monkeypatch):
    """abort() on the way out of every reply would cut the last slice short,
    which is a word clipped off the end of every answer."""
    sp, stream = _speaker(monkeypatch, seconds=0.2)
    produced = sp.say("short")

    assert stream.aborted == 0, "an uninterrupted reply must play to its end"
    assert abs(produced - 0.2) < 0.01, f"it lost audio: {produced}"


def test_the_watch_is_consulted_while_a_sentence_plays(monkeypatch):
    """Barge-in's path, and the one the lock now uses. It used to be called
    once per sentence, which is the same as not at all for a ten-second one."""
    sp, stream = _speaker(monkeypatch, seconds=5.0)

    calls = []
    sp.say("a sentence", watch=lambda: bool(calls.append(1)) or len(calls) >= 4)

    assert len(calls) == 4, f"watch was consulted {len(calls)} times in 5s of audio"
    assert len(stream.writes) == 4
    assert stream.aborted == 1
