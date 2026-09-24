"""Hearing the user over our own voice, when the room allows it.

Measured on the machine this was written on: every gate that detects the
user also fires on our own output, because at a normal listening volume the
two arrive at the microphone at the same level. So this ships off by
default. These tests are about the mechanism being correct, not about it
being usable everywhere -- the second is a property of the room.
"""
from __future__ import annotations

import pytest

# Both skips before any heavy import: the runner installs pytest and nothing
# else, and an import above the skip fails before the skip can help.
np = pytest.importorskip("numpy", reason="the runner carries pytest only")
barge = pytest.importorskip("barge", reason="needs openWakeWord for Silero")
BargeIn, VAD_FRAME = barge.BargeIn, barge.VAD_FRAME


def tone(seconds: float, amplitude: float, rate: int = 16000) -> bytes:
    """Speech-shaped enough for an energy gate; not for the VAD."""
    n = int(seconds * rate)
    t = np.arange(n) / rate
    wave = np.sin(2 * np.pi * 180 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t))
    return (wave * amplitude * 32767).astype(np.int16).tobytes()


class TestGate:
    def test_silence_never_fires(self):
        b = BargeIn()
        assert not b.feed(tone(2.0, 0.0))

    def test_the_first_frames_calibrate_rather_than_trigger(self):
        """Our own voice starts the utterance; it must set the bar, not clear
        it. Otherwise every reply interrupts itself on its first syllable."""
        b = BargeIn()
        loud = tone(WARM := 0.2, 0.3)
        assert not b.feed(loud), "the calibration window itself triggered"

    def test_a_quiet_room_after_calibration_stays_quiet(self):
        b = BargeIn()
        b.feed(tone(0.2, 0.3))          # calibrate on our own bleed
        assert not b.feed(tone(1.0, 0.001))

    def test_the_gate_sits_above_the_measured_bleed(self):
        b = BargeIn(factor=2.0)
        b.feed(tone(0.2, 0.2))
        b.feed(tone(0.2, 0.05))
        assert b.last_gate > 0, "no gate was ever computed"
        assert b.last_gate >= b.min_rms

    def test_the_user_cannot_raise_the_bar_they_must_clear(self):
        """Only frames below the gate update the bleed estimate. Without
        that, talking over it teaches it that loud is normal and the gate
        climbs out of reach."""
        b = BargeIn()
        b.feed(tone(0.2, 0.02))
        before = b._bleed
        b.feed(tone(1.0, 0.9))          # someone shouting
        assert b._bleed <= before * 1.01


class TestConfiguration:
    def test_a_higher_factor_demands_a_louder_voice(self):
        quiet, loud = BargeIn(factor=1.5), BargeIn(factor=4.0)
        for b in (quiet, loud):
            b.feed(tone(0.2, 0.05))
            b.feed(tone(0.1, 0.05))
        assert loud.last_gate > quiet.last_gate

    def test_the_floor_applies_when_there_is_no_bleed(self):
        b = BargeIn(min_rms=0.05)
        b.feed(tone(0.2, 0.0))
        b.feed(tone(0.1, 0.0))
        assert b.last_gate == pytest.approx(0.05)

    def test_reset_forgets_the_previous_utterance(self):
        """Each reply is calibrated afresh: the bleed depends on what is
        being said and how loudly."""
        b = BargeIn()
        b.feed(tone(0.5, 0.4))
        b.reset()
        assert b._bleed == 0.0 and b._warm > 0
