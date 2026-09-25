"""How the contrast set is counted.

A verifier needs negatives -- clips that are not this person saying the wake
phrase -- and the trainer assembles them from two sources: any corpus already
on disk, and other Piper voices synthesised saying the phrase. It then gated
on the sum of what the two steps reported.

Both steps report totals, not additions, and synth writes deterministic
filenames. So a second run counted the same four clips twice, cleared a gate
that needs five, let the user record twenty-five positives, and only then
failed inside train() on the four that were really there.
"""
from __future__ import annotations

import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _wav(path: Path, seconds: float = 0.5, rate: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as h:
        h.setnchannels(1)
        h.setsampwidth(2)
        h.setframerate(rate)
        h.writeframes(b"\0\0" * int(rate * seconds))


def _gather():
    pytest.importorskip("numpy")           # verifier.py imports it at module level
    from verifier import gather_negatives
    return gather_negatives


def test_gather_reports_the_whole_directory_not_what_it_added(tmp_path):
    """The number that misled the caller. Documented so the sum stays wrong."""
    gather_negatives = _gather()
    neg = tmp_path / "negatives"
    for i in range(4):
        _wav(neg / f"other-en_US-lessac-medium-{i}.wav")

    # Nothing new to copy, yet it reports four -- the four already there.
    assert gather_negatives(neg, []) == 4


def test_gather_does_not_recount_a_clip_it_already_copied(tmp_path):
    gather_negatives = _gather()
    neg = tmp_path / "negatives"
    src = tmp_path / "corpus"
    _wav(src / "speech-0.wav")
    _wav(src / "speech-1.wav")

    first = gather_negatives(neg, [src])
    second = gather_negatives(neg, [src])

    assert first == 2
    assert second == 2, "copying nothing new must not raise the count"
    assert len(list(neg.glob("*.wav"))) == 2


def test_gather_skips_clips_the_feature_extractor_cannot_use(tmp_path):
    """A 44.1kHz stereo clip counted but was never copied, so the count and
    the directory disagreed -- the same class of mismatch as the double count."""
    gather_negatives = _gather()
    neg = tmp_path / "negatives"
    src = tmp_path / "corpus"
    _wav(src / "good.wav", rate=16000)
    with wave.open(str(src / "wrong-rate.wav"), "wb") as h:
        h.setnchannels(2)
        h.setsampwidth(2)
        h.setframerate(44100)
        h.writeframes(b"\0\0\0\0" * 1000)

    assert gather_negatives(neg, [src]) == 1
    assert len(list(neg.glob("*.wav"))) == 1


def test_the_trainer_gates_on_the_directory_rather_than_the_sum():
    """The fix, asserted where it lives.

    The front end is bash and needs a microphone to run end to end, so this
    checks the one line that matters: the gate counts files, and does not add
    the two reported numbers together.
    """
    script = (ROOT / "bin" / "agentvoice-train-verifier").read_text()
    assert 'NEG_COUNT=$(find "$NEG" -name \'*.wav\' 2>/dev/null | wc -l)' in script
    assert "NEG_COUNT + OTHERS" not in script, "back to summing two totals"


def test_the_trainer_does_not_send_people_to_files_it_never_installs():
    """install.sh copies daemon, bin and desktop -- not bench. The failure
    message told users to run `python bench/record.py`, which on any real
    install does not exist."""
    script = (ROOT / "bin" / "agentvoice-train-verifier").read_text()
    gate = script[script.index("Only $NEG_COUNT contrast clips"):]
    gate = gate[:gate.index("exit 1")]
    assert "bench/record.py" not in gate
    assert "install.sh" in gate, "must say how to actually fix it"


def test_the_trainer_offers_other_speakers_before_it_refuses():
    """A default install has one voice and cannot reach five clips, so the
    stock install could not train a verifier at all. The trainer offers to
    fetch other speakers, and only refuses if that is declined or fails."""
    script = (ROOT / "bin" / "agentvoice-train-verifier").read_text()
    offer = script.index("Fetch ${#WANT[@]} more voices")
    refusal = script.index("Only $NEG_COUNT contrast clips")
    assert offer < refusal, "refuses before offering a way through"

    # Both-or-neither, so a voice whose .json never arrived cannot load as a
    # stack trace inside the spinner.
    fetch = script[script.index("fetch_speaker() {"):]
    fetch = fetch[:fetch.index("\n}")]
    assert fetch.count("curl -fsSL") == 2
    assert "&&" in fetch, "the two downloads must be chained, not independent"
    assert fetch.count("rm -rf \"$tmp\"") == 2, "must clean up on both paths"


def test_the_contrast_voices_are_distinct_speakers():
    """Four clips from one voice at different speeds is not 'wrong speaker'.
    The list must name several real Piper speakers, not qualities of one."""
    script = (ROOT / "bin" / "agentvoice-train-verifier").read_text()
    line = next(l for l in script.splitlines() if l.startswith("OTHER_SPEAKERS="))
    ids = line.split("(", 1)[1].rstrip(")").split()
    speakers = {i.rsplit("-", 1)[0] for i in ids}
    assert len(speakers) >= 3, f"only {speakers}"
    assert all("-" in i for i in ids), "each needs a quality suffix for the URL"
