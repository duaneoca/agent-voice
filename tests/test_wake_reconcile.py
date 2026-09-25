"""Changing the detection threshold has to change the detection threshold.

Pipeline.apply() rebuilds the wake engine only when its spec changes -- engine,
model or phrase, and the verifier's mtime. openWakeWord's firing threshold was
not in that spec and was not assigned afterwards either: it lived on the engine
object, set once when the engine was constructed.

So dragging the slider printed "knobs updated" and did nothing. A threshold of
90, which a locally trained model cannot reach (FINDINGS §15), survived being
set to 63 and then to 51, and the wake word never fired once. The cure people
found was switching the engine to Vosk and back, which does change the spec.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))


class FakeOww:
    """Stands in for the engine: only the threshold matters here, and building
    the real one needs onnxruntime plus a model on disk."""
    def __init__(self, threshold: float) -> None:
        self.threshold = threshold
        self.phrase = "hey claude"
        self.key = "hey_claude"
        self.verifier = None


def _pipeline_with(monkeypatch, threshold_pct: int):
    import wake_listen
    pipe = object.__new__(wake_listen.Pipeline)
    pipe._oww = FakeOww(0.90)
    pipe._wake_spec = ("openwakeword", "hey_claude", 0.0)
    pipe._whisper_size = "tiny.en"
    pipe._whisper_override = None
    pipe._partials = "auto"

    cfg = {
        "engine": "openwakeword", "owwModel": "hey_claude",
        "owwThresholdPct": threshold_pct, "micThresholdDb": -40,
        "useVerifier": True, "phrase": "hey computer",
        "leadInMs": 5000, "trailingSilenceMs": 1200, "minUtteranceMs": 300,
        "maxUtteranceMs": 15000, "refractoryMs": 1500, "wakeConfidence": 0.70,
        "model": "tiny.en", "livePartials": "auto",
        "conversationMode": True, "followUpMs": 7000,
    }

    class Cfg(dict):
        def str(self, k): return str(self[k])
        def int(self, k): return int(self[k])
        def bool(self, k): return bool(self[k])

    return pipe, Cfg(cfg)


def _settled(monkeypatch, threshold_pct: int):
    """A pipeline whose spec already matches its config.

    Hand-writing the spec tuple in the fixture worked until the tuple gained a
    field, at which point every test here rebuilt the engine for real and died
    reaching for Vosk. Letting apply() set its own spec keeps these tests about
    what they are testing.
    """
    pipe, cfg = _pipeline_with(monkeypatch, threshold_pct)
    rebuilt: list[str] = []
    pipe._build_wake = lambda c, e: rebuilt.append(e)
    pipe.apply(cfg)
    rebuilt.clear()
    return pipe, cfg, rebuilt


def test_lowering_the_threshold_reaches_the_running_engine(monkeypatch):
    pipe, cfg, _ = _settled(monkeypatch, 90)
    assert pipe._oww.threshold == pytest.approx(0.90)

    cfg["owwThresholdPct"] = 51
    pipe.apply(cfg)

    assert pipe._oww.threshold == pytest.approx(0.51), \
        "the slider moved and the engine did not"


def test_the_engine_is_not_rebuilt_for_a_threshold_change(monkeypatch):
    """A rebuild would drop and reload ~100MB of model for a slider move, so
    the threshold is assigned rather than added to the spec."""
    pipe, cfg, rebuilt = _settled(monkeypatch, 90)

    cfg["owwThresholdPct"] = 51
    pipe.apply(cfg)

    assert rebuilt == [], f"rebuilt for a number: {rebuilt}"
    assert pipe._oww.threshold == pytest.approx(0.51)


def test_turning_the_verifier_off_does_rebuild(monkeypatch):
    """The opposite case, and the reason the threshold is not in the spec:
    openWakeWord takes the verifier as a constructor argument, so switching it
    off means building the engine without it."""
    pipe, cfg, rebuilt = _settled(monkeypatch, 51)

    cfg["useVerifier"] = False
    pipe.apply(cfg)

    assert rebuilt == ["openwakeword"], "a verifier change needs a new engine"


def test_raising_it_reaches_the_engine_too(monkeypatch):
    pipe, cfg, _ = _settled(monkeypatch, 51)
    cfg["owwThresholdPct"] = 85
    pipe.apply(cfg)
    assert pipe._oww.threshold == pytest.approx(0.85)


def test_nothing_breaks_when_openwakeword_is_not_the_engine(monkeypatch):
    pipe, cfg = _pipeline_with(monkeypatch, 51)
    pipe._oww = None
    pipe._wake_spec = ("vosk", "hey computer", 0.0)
    cfg["engine"] = "vosk"
    cfg["phrase"] = "hey computer"
    pipe._build_wake = lambda c, e: None

    pipe.apply(cfg)                       # must not raise

    assert pipe.detection_threshold is None


def test_the_banner_reports_the_number_that_governs_the_engine(monkeypatch):
    """It printed Vosk's grammar confidence while running openWakeWord, so the
    number that did nothing was on screen and the stale one was not."""
    pipe, cfg, _ = _settled(monkeypatch, 51)
    assert pipe.detection_threshold == pytest.approx(0.51)

    pipe._oww = None
    assert pipe.detection_threshold is None
