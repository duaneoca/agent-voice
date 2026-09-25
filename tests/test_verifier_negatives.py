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


# --- the threshold, which is what actually kept the light off ---------------
# The verifier trained fine and the wake word still never fired. openWakeWord's
# default threshold of 90 suits the four pretrained phrases, which peak near
# 0.996; a locally trained model peaked at a median of 0.775 with its verifier,
# so none of the speaker's twenty-five recordings would ever have woken it.

def test_the_noise_floor_keeps_failed_clips_out_of_the_suggestion():
    """Six of twenty-five real clips scored 0.005 -- the phrase clipped by the
    recording window, a false start, a cough. Counting them as evidence that
    the threshold should be lower drove the first version to its floor, which
    would fire on a television."""
    pytest.importorskip("numpy")
    import verifier

    # The distribution actually measured, duds included.
    peaks = [0.005] * 6 + [0.61, 0.68, 0.73, 0.74, 0.775, 0.78, 0.79, 0.80,
                           0.81, 0.82, 0.83, 0.84, 0.85, 0.86, 0.866, 0.867,
                           0.868, 0.868, 0.868]
    usable = [p for p in peaks if p >= verifier.NOISE_FLOOR]
    assert len(usable) == 19

    ordered = sorted(usable, reverse=True)
    cut = ordered[min(int(len(ordered) * 0.9), len(ordered) - 1)]
    pct = max(45, min(90, int(cut * 100) - 5))

    assert pct == 63, pct
    assert sum(1 for p in peaks if p >= pct / 100) == 18
    assert pct > 45, "must not land on the floor when there are real clips"


def test_a_model_that_never_responds_is_reported_not_papered_over():
    """If nothing cleared the noise floor, no threshold helps -- the model is
    wrong for these recordings, and lowering it would only add false wakes."""
    pytest.importorskip("numpy")
    import verifier
    assert all(p < verifier.NOISE_FLOOR for p in [0.01, 0.005, 0.1])


def test_the_suggestion_is_clamped_at_both_ends():
    """Never above the default, because that can only make it harder to wake;
    never so low it fires on ordinary speech."""
    for cut in (0.99, 0.95, 0.50, 0.30, 0.21):
        pct = max(45, min(90, int(cut * 100) - 5))
        assert 45 <= pct <= 90


def test_the_trainer_measures_the_threshold_after_training():
    script = (ROOT / "bin" / "agentvoice-train-verifier").read_text()
    trained = script.index('"$VERIFIER" train ')
    measured = script.index("suggest-threshold")
    assert trained < measured, "measure the trained model, not the untrained one"
    # And it must ask rather than write the setting behind the user's back.
    offer = script.index("Set the wake threshold to")
    write = script.index("owwThresholdPct \"$WANT_PCT\"")
    assert offer < write


def test_the_trainer_reads_the_current_threshold_from_shell_json():
    """`omarchy bar` has set but no get; asking it anyway silently yielded the
    default, so the trainer would offer to change a value it had not read."""
    script = (ROOT / "bin" / "agentvoice-train-verifier").read_text()
    assert "omarchy bar get" not in script
    assert "shell.json" in script


def test_a_numeric_setting_is_written_as_a_number():
    """`omarchy bar set` feeds `jq --argjson`, so a number needs --json and a
    bare string must not have it. Written without, the measured threshold landed
    in shell.json as "63" while every other numeric setting was a number."""
    script = (ROOT / "bin" / "agentvoice-train-verifier").read_text()
    for line in script.splitlines():
        if "omarchy bar set" not in line or line.strip().startswith("#"):
            continue
        key = line.split("duaneoca.agentvoice", 1)[1].split()[0]
        numeric = key.endswith("Pct") or key.endswith("Db") or key.endswith("Ms")
        assert ("--json" in line) == numeric, \
            f"{key} is {'numeric' if numeric else 'a string'}: {line.strip()}"
