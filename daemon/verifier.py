"""Train a personal wake-word verifier, and load one at runtime.

The pretrained openWakeWord models are speaker-independent by design. That is
right for a general detector and wrong for a microphone in your living room:
a podcast saying the wake phrase produces the correct phonemes in the wrong
voice, and nothing else in the pipeline can tell the difference. The energy
gate only helps if the other speaker is quiet, and a grammar's confidence is
no help at all -- measured on this machine, Vosk scored "hey cloud" 1.00
against a "hey claude" grammar.

A verifier is the only speaker-aware layer available. It is a small logistic
regression over openWakeWord's own embeddings, trained on clips of you saying
the phrase against clips of other speech, and it runs only after the primary
model has already fired -- replacing that score with its own probability.

Training data is deliberately cheap: a couple of dozen clips is enough,
because the classifier is drawing a boundary in an embedding space the wake
model already learned rather than learning speech from scratch.
"""
from __future__ import annotations

import os
import warnings

# openWakeWord asks onnxruntime for a CUDA provider it will not find on a CPU
# box, and onnxruntime answers with a UserWarning that reads like a failure.
# It is noise on every machine this is aimed at, and it lands in the middle of
# a progress spinner, so it is filtered at the source rather than redirected
# away -- redirecting would also hide real errors.
warnings.filterwarnings(
    "ignore",
    message=r".*Specified provider 'CUDAExecutionProvider' is not in available provider names.*",
    category=UserWarning,
)
import shutil
import wave
from pathlib import Path

from paths import (  # noqa: F401
    DATA_DIR, VERIFIERS as VERIFIER_DIR, WAKEWORDS, piper_voices,
)

RATE = 16_000

#: openWakeWord ships these; a verifier is trained against one of them by name.
PRETRAINED = ("hey_jarvis", "alexa", "hey_mycroft", "hey_marvin")


def resolve_model(name: str) -> tuple[Path, str]:
    """Map a friendly wake-word name to its bundled file and its runtime key.

    openWakeWord names models differently depending on how they were loaded,
    and a verifier is keyed by that name -- so getting it wrong fails at
    runtime with an unhelpful "some were not matched with a base model":

        Model()                                  -> "hey_jarvis"
        Model(wakeword_model_paths=[".../hey_jarvis_v0.1.onnx"])
                                                 -> "hey_jarvis_v0.1"

    agentvoice always loads by explicit path (loading by name pulls in all
    five bundled models), so the key is the file stem. Training is given the
    same path, so both sides agree.

    A phrase the user trained themselves is looked for first, in
    ~/.local/share/agentvoice/wakewords. The settings screen has always
    offered those in its dropdown, and this did not know they existed -- so
    choosing one failed here, and the verifier trainer could not be pointed
    at it either. Trained wake words are the whole reason that directory is
    documented; they should not be second-class to the four in the wheel.
    """
    if name.endswith(".onnx"):
        path = Path(name)
        return path, path.stem

    mine = WAKEWORDS / f"{name}.onnx"
    if mine.exists():
        return mine, mine.stem

    # Imported here rather than at the top, and only once the bundled models
    # are actually needed: a phrase you trained yourself is resolved without
    # openWakeWord being importable at all, which is true of any machine that
    # installed without the optional extra -- and of the test runner.
    try:
        import openwakeword
    except ImportError:
        raise ClipProblem(
            f"no wake model named {name!r} in {WAKEWORDS}, and openWakeWord "
            f"is not installed to look for a bundled one") from None

    base = Path(openwakeword.__file__).parent / "resources/models"
    matches = (sorted(base.glob(f"{name}_v*.onnx"))
               or sorted(base.glob(f"{name}.onnx")))
    if not matches:
        raise ClipProblem(f"no wake model named {name!r} in {WAKEWORDS} or {base}")
    return matches[0], matches[0].stem


class ClipProblem(Exception):
    """A recorded clip is unusable and should be retaken."""


def clip_health(path: Path) -> str | None:
    """Return a human reason the clip is unusable, or None when it is fine.

    Clipped audio cannot be repaired, and a verifier trained on distorted
    positives learns the distortion. Catching it while the person is still
    sitting at the microphone is the only cheap moment.
    """
    import numpy as np
    with wave.open(str(path), "rb") as handle:
        pcm = handle.readframes(handle.getnframes())
    if not pcm:
        return "no audio captured"
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    duration = len(x) / RATE
    peak = float(np.abs(x).max()) / 32768
    rms = float(np.sqrt((x ** 2).mean())) / 32768
    clipped = float((np.abs(x) >= 32700).mean())
    if duration < 0.35:
        return "too short — hold the key a moment longer"
    if clipped > 0.001:
        return f"clipping ({clipped:.1%} of samples) — lower the capture gain"
    if rms < 0.004:
        return "too quiet — move closer or raise the gain"
    if peak < 0.05:
        return "almost silent — did the microphone pick you up?"
    return None


def record_clip(path: Path, seconds: float, device: int | None = None) -> float:
    """Record a fixed window. Returns measured RMS in dBFS, for a live readout."""
    import numpy as np
    import sounddevice as sd

    frames = sd.rec(int(seconds * RATE), samplerate=RATE, channels=1,
                    dtype="int16", device=device)
    sd.wait()
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(frames.tobytes())
    x = frames.astype(np.float32).ravel()
    rms = float(np.sqrt((x ** 2).mean())) / 32768
    return 20.0 * np.log10(rms + 1e-9)


def train(model_name: str, positives: Path, negatives: Path,
          output: Path | None = None) -> Path:
    """Fit the verifier and write it next to the other agentvoice state."""
    from openwakeword import train_custom_verifier

    pos = sorted(positives.glob("*.wav"))
    neg = sorted(negatives.glob("*.wav"))
    if len(pos) < 10:
        raise ClipProblem(f"only {len(pos)} positive clips; 20 or more is much better")
    if len(neg) < 5:
        raise ClipProblem(f"only {len(neg)} negative clips; need other speech to contrast against")


    model_path, key = resolve_model(model_name)
    output = output or (VERIFIER_DIR / f"{key}.joblib")
    output.parent.mkdir(parents=True, exist_ok=True)
    # The upstream docstring says these take "the path to a directory". They
    # do not -- the implementation iterates them as lists of file paths, and
    # passing a directory string makes it open the string's first character
    # ("/") as a WAV. Pass explicit file lists.
    try:
        train_custom_verifier(
            positive_reference_clips=[str(p) for p in pos],
            negative_reference_clips=[str(p) for p in neg],
            output_path=str(output),
            # The path form, so the verifier is keyed by the file stem that
            # path-loading produces at runtime.
            model_name=str(model_path),
        )
    except ValueError as e:
        if "positive features" in str(e):
            raise ClipProblem(
                f"the {key} model did not recognise the wake phrase in any "
                "of your clips, so there was nothing to learn from. Check you said "
                "the right phrase, and that the microphone level looked healthy."
            ) from e
        raise
    # Positives only contribute features from frames the base model already
    # scores above 0.5, so a clip it never hears contributes nothing. Upstream
    # signals that with a ValueError whose message has the sense inverted
    # ("The positive features were created!"), which is worth translating.
    return output


def installed_verifiers() -> dict[str, Path]:
    """Trained verifiers on this machine, keyed by wake model name."""
    if not VERIFIER_DIR.is_dir():
        return {}
    return {p.stem: p for p in sorted(VERIFIER_DIR.glob("*.joblib"))}


def gather_negatives(into: Path, extra_sources: list[Path] | None = None) -> int:
    """Collect 'other speech' clips, reusing any corpus already on disk.

    The benchmark corpus is ideal: recordings of this person talking, on this
    microphone, containing no wake phrase at all.
    """
    into.mkdir(parents=True, exist_ok=True)
    count = len(list(into.glob("*.wav")))
    for source in extra_sources or []:
        if not source.is_dir():
            continue
        for wav in sorted(source.glob("*.wav")):
            target = into / f"corpus-{wav.name}"
            if target.exists():
                continue
            try:
                with wave.open(str(wav)) as handle:
                    if handle.getframerate() != RATE or handle.getnchannels() != 1:
                        continue
            except Exception:
                continue
            shutil.copy2(wav, target)
            count += 1
    return count


def synth_other_speakers(phrase: str, into: Path, voices_dir: Path,
                         per_voice: int = 4) -> int:
    """Generate negatives of OTHER voices saying the wake phrase.

    This is the case that actually fails in a living room: a podcast or a
    phone says the right words in the wrong voice, and the base model -- which
    is speaker-independent by design -- fires. Negatives made only of
    miscellaneous speech teach the verifier "phrase versus no phrase", which
    the base model already knows. Negatives of other people saying the phrase
    teach it the thing we need: "right words, wrong speaker, reject".

    The Piper voices already on disk for TTS double as the other speakers, so
    this costs nothing but a few seconds of synthesis.
    """
    from piper import PiperVoice
    from piper.config import SynthesisConfig

    into.mkdir(parents=True, exist_ok=True)
    made = 0
    for onnx in sorted(voices_dir.glob("*.onnx")):
        voice = PiperVoice.load(str(onnx))
        rate = getattr(voice.config, "sample_rate", RATE)
        for i in range(per_voice):
            cfg = SynthesisConfig(length_scale=0.9 + 0.1 * i,
                                  noise_scale=0.5 + 0.12 * i)
            pcm = b"".join(c.audio_int16_bytes for c in voice.synthesize(phrase, cfg))
            if rate != RATE:
                # Most Piper voices are 22050Hz and the feature extractor
                # wants 16k. scipy is already a hard dependency of
                # openWakeWord, so resampling costs nothing extra.
                import numpy as np
                from scipy.signal import resample_poly
                x = np.frombuffer(pcm, dtype=np.int16)
                g = np.gcd(RATE, rate)
                y = resample_poly(x.astype(np.float32), RATE // g, rate // g)
                pcm = np.clip(y, -32768, 32767).astype(np.int16).tobytes()
            target = into / f"other-{onnx.stem}-{i}.wav"
            with wave.open(str(target), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(RATE)
                handle.writeframes(pcm)
            made += 1
    return made


# --- CLI, so the bash front end can drive each step separately -------------

def main() -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("record", help="record one clip and report its health")
    p.add_argument("path", type=Path)
    p.add_argument("--seconds", type=float, default=2.0)
    p.add_argument("--device", type=int, default=None)

    p = sub.add_parser("negatives", help="collect 'other speech' clips")
    p.add_argument("into", type=Path)
    p.add_argument("--from", dest="sources", type=Path, nargs="*", default=[])

    p = sub.add_parser("synth-negatives",
                       help="synthesize other voices saying the wake phrase")
    p.add_argument("phrase")
    p.add_argument("into", type=Path)
    p.add_argument("--voices", type=Path, default=None)
    p.add_argument("--per-voice", type=int, default=4)

    p = sub.add_parser("train", help="fit the verifier")
    p.add_argument("model_name")
    p.add_argument("positives", type=Path)
    p.add_argument("negatives", type=Path)
    p.add_argument("--output", type=Path, default=None)

    sub.add_parser("list", help="show trained verifiers")
    sub.add_parser("models", help="show wake models a verifier can attach to")

    args = ap.parse_args()

    if args.cmd == "record":
        level = record_clip(args.path, args.seconds, args.device)
        problem = clip_health(args.path)
        # Two lines, so the caller can show the level and act on the verdict.
        print(f"{level:.1f}")
        if problem:
            print(problem)
            args.path.unlink(missing_ok=True)
            return 1
        return 0

    if args.cmd == "negatives":
        print(gather_negatives(args.into, args.sources))
        return 0

    if args.cmd == "synth-negatives":
        print(synth_other_speakers(args.phrase, args.into,
                                   args.voices or piper_voices(), args.per_voice))
        return 0

    if args.cmd == "train":
        try:
            out = train(args.model_name, args.positives, args.negatives, args.output)
        except ClipProblem as e:
            print(str(e), file=sys.stderr)
            return 2
        print(out)
        return 0

    if args.cmd == "list":
        found = installed_verifiers()
        for name, path in found.items():
            print(f"{name}\t{path}")
        return 0 if found else 1

    if args.cmd == "models":
        # Your own first: if you trained one, it is the one you mean.
        for path in sorted(WAKEWORDS.glob("*.onnx")):
            print(path.stem)
        for name in PRETRAINED:
            print(name)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
