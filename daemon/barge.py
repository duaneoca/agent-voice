"""Hearing you over ourselves, when the room allows it.

Not wake-word detection through the bleed: that was measured at 5/20 on this
hardware and the reason is in bench/FINDINGS.md -- at a normal listening
volume our own playback reaches the microphone as loudly as the speaker does.
Recognising a phrase through that needs about 24 dB of headroom that is not
there.

This asks a much smaller question instead, the one telephony has always
asked: is that speech, and is it louder than the echo? A voice activity
detector answers the first half and an energy gate calibrated to our own
bleed answers the second. The approach is Atzingen/hey-jarvis's; the numbers
below are ours.

It is off by default, because on the machine it was written on it does not
work: every gate that hears the user also fires on our own output, since the
two arrive at the same level. Somewhere with a headset, or a microphone that
is not beside the speaker, it works well -- so it is offered with a way to
find out rather than a promise. `agentvoice calibrate` says which you have.
"""
from __future__ import annotations

import pathlib

import numpy as np

#: Silero, 512 samples at 16k. Ships inside openWakeWord, so this costs no
#: new dependency on a machine that already chose that engine.
VAD_FRAME = 512
VAD_SPEECH = 0.5

#: Defaults are ours, not upstream's. Theirs floor the gate at 0.012 RMS
#: (-38.4 dBFS), which is below the measured bleed here, so out of the box it
#: interrupts itself within a second.
DEFAULT_MIN_RMS = 0.012
DEFAULT_FACTOR = 1.5
WARMUP_FRAMES = 4
HITS_NEEDED, HITS_WINDOW = 3, 4


class BargeIn:
    """Watches for the user talking over us. Fed while we speak."""

    def __init__(self, min_rms: float = DEFAULT_MIN_RMS,
                 factor: float = DEFAULT_FACTOR) -> None:
        import onnxruntime as ort
        import openwakeword

        model = (pathlib.Path(openwakeword.__file__).parent
                 / "resources/models/silero_vad.onnx")
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._vad = ort.InferenceSession(str(model), sess_options=opts,
                                         providers=["CPUExecutionProvider"])
        self.min_rms = min_rms
        self.factor = factor
        self.reset()

    def reset(self) -> None:
        """Called at the start of each utterance: the bleed is recalibrated
        from scratch, because it depends on what is being said and how loud."""
        self._h = np.zeros((2, 1, 64), np.float32)
        self._c = np.zeros((2, 1, 64), np.float32)
        self._tail = np.zeros(0, np.float32)
        self._bleed = 0.0
        self._warm = WARMUP_FRAMES
        self._hits: list[int] = []
        self.last_rms = 0.0
        self.last_gate = 0.0

    def _speech(self, frame: np.ndarray) -> float:
        out, self._h, self._c = self._vad.run(
            None, {"input": frame.reshape(1, -1).astype(np.float32),
                   "sr": np.array(16000, dtype=np.int64),
                   "h": self._h, "c": self._c})
        return float(out.max())

    def feed(self, pcm: bytes) -> bool:
        """True when the user is talking over us and we should stop."""
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        self._tail = np.concatenate([self._tail, samples])
        while len(self._tail) >= VAD_FRAME:
            frame, self._tail = self._tail[:VAD_FRAME], self._tail[VAD_FRAME:]
            rms = float(np.sqrt(np.mean(frame * frame)))
            self.last_rms = rms
            if self._warm > 0:
                # The first frames of our own speech are the bleed level.
                self._bleed = max(self._bleed, rms)
                self._warm -= 1
                continue
            gate = max(self._bleed * self.factor, self.min_rms)
            self.last_gate = gate
            if rms < gate:
                # Only quiet frames update the estimate, so the user's voice
                # can never raise the bar it has to clear.
                self._bleed = max(self._bleed * 0.97, rms)
            self._hits.append(1 if (rms >= gate and self._speech(frame) >= VAD_SPEECH) else 0)
            self._hits = self._hits[-HITS_WINDOW:]
            if sum(self._hits) >= HITS_NEEDED:
                return True
        return False


def calibrate(seconds: float = 8.0, device=None) -> int:
    """Measure whether barge-in can work here, and say so plainly.

    Speaks, records our own bleed off the microphone, then asks the user to
    talk while it listens again. The answer is the gap between the two: if
    our voice and theirs arrive at the same level, no threshold separates
    them and no amount of tuning will.
    """
    import time

    import sounddevice as sd

    sys_path_hack = pathlib.Path(__file__).resolve().parent
    import sys
    if str(sys_path_hack) not in sys.path:
        sys.path.insert(0, str(sys_path_hack))
    from runtime import Config, Speaker

    cfg = Config()
    rate, chunk = 16000, 3200
    frames: list[bytes] = []

    def level(buf: list[bytes]) -> tuple[float, float]:
        x = np.frombuffer(b"".join(buf), dtype=np.int16).astype(np.float32)
        if not len(x):
            return 0.0, 0.0
        per = [np.sqrt(np.mean(x[i:i+1280] ** 2)) / 32768.0
               for i in range(0, len(x) - 1280, 1280)]
        return float(np.median(per)), float(np.percentile(per, 90))

    speaker = Speaker(cfg.str("voice"))
    stream = sd.RawInputStream(samplerate=rate, channels=1, dtype="int16",
                               blocksize=chunk, device=device,
                               callback=lambda i, n, t, s: frames.append(bytes(i)))

    print("\n  Measuring our own voice as the microphone hears it. Stay quiet.")
    stream.start()
    speaker.say("I am measuring how loudly my own voice reaches the microphone. "
                "Please stay quiet until I stop talking.")
    time.sleep(0.2)
    stream.stop()
    bleed_med, bleed_p90 = level(frames)

    frames.clear()
    print(f"  Now say something, in your normal voice, for {seconds:.0f} seconds.")
    stream.start()
    time.sleep(seconds)
    stream.stop()
    stream.close()
    voice_med, voice_p90 = level(frames)

    def db(v):
        return 20 * np.log10(max(v, 1e-9))

    headroom = db(voice_p90) - db(bleed_p90)
    print(f"\n    our own voice, as heard   {db(bleed_med):6.1f} dBFS   (peaks {db(bleed_p90):.1f})")
    print(f"    your voice                {db(voice_med):6.1f} dBFS   (peaks {db(voice_p90):.1f})")
    print(f"    headroom                  {headroom:+6.1f} dB\n")

    if headroom >= 9:
        factor = min(3.0, max(1.5, 10 ** (headroom / 40)))
        print(f"  Barge-in should work here. Suggested sensitivity: {factor:.1f}")
        print(f"    omarchy bar set duaneoca.agentvoice bargeFactor {int(factor * 100)} --json")
        print( "    omarchy bar set duaneoca.agentvoice bargeIn true --json")
        return 0
    print("  Barge-in will not work here. Your voice and ours arrive at the")
    print("  microphone at about the same level, so no threshold separates")
    print("  them: every setting that hears you also fires on us. A headset,")
    print("  or moving the microphone away from the speaker, is the fix.")
    print("  The microphone stays deaf while it speaks; press F8 to interrupt.")
    return 1
