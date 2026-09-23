# Benchmark findings

Measured 2026-09-21 on the development machine: MacBookPro11,5 (2014),
i7-4980HQ, 4 cores / 8 threads, AVX2 without AVX-512, 16GB, no usable ML GPU.
Corpus: 17 clips, 98s, read aloud by the project author into the built-in
laptop microphone.

These numbers describe *this* machine. Re-run `bench/` anywhere else before
trusting them.

## 1. Custom vocabulary beats model size, decisively

A fourteen-word `initial_prompt` outperforms a six-times-larger model at a
sixth of the latency.

| config | overall WER | domain WER | perceived latency |
|---|---|---|---|
| whisper tiny.en | 9.8% | 27.2% | 317ms |
| **whisper tiny.en + vocab** | **4.9%** | **9.9%** | **358ms** |
| whisper small.en, no vocab | 7.8% | 18.6% | 2165ms |

Confirmed independently through Voxtype's whisper.cpp (f16 rather than
faster-whisper's int8, so absolute numbers differ, conclusion does not):
tiny.en 11.0% -> 6.5% with the prompt; base.en + vocab lands at 6.9%.

Custom vocabulary is therefore a first-class component, not a tuning detail.
It ships as `daemon/vocab.txt`, overridable at `~/.config/agentvoice/vocab.txt`.

## 2. Ordinary speech is already solved; jargon is the whole problem

WER by category, no vocabulary prompt:

| engine | short | plain | domain | long |
|---|---|---|---|---|
| vosk-small | 10.0% | 2.5% | **39.2%** | 10.5% |
| whisper tiny.en | 0.0% | 0.0% | **28.2%** | 2.6% |
| whisper base.en | 0.0% | 1.2% | **25.1%** | 2.6% |
| whisper small.en | 0.0% | 2.6% | **18.6%** | 2.6% |

tiny.en scores 0.0% on conversational speech. Every remaining error is a
proper noun. No engine at any size transcribed `kubectl` correctly:
"cook controlled", "coupe control", "koop control", "Koopkertrol".

## 3. Rank on perceived latency, not real-time factor

Vosk consumes audio while you are still speaking, so only the tail remains
when you stop. Whisper cannot start until the utterance ends, so its whole
run is dead air. whisper-tiny has the **better** RTF (0.13 vs 0.23) and **ten
times** the perceived latency (323ms vs 30ms). Published RTF rankings pick the
wrong engine for this workload.

Voxtype's `whisper.eager_processing` ("transcribe overlapping chunks while you
are still speaking") is the mitigation and is untested here.

## 4. TTS has margin to spare

| engine | short reply | 190-char paragraph | RTF |
|---|---|---|---|
| espeak-ng | 6ms | 15ms | 0.002 |
| piper lessac-low | 18ms | 332ms | 0.034 |
| **piper lessac-medium** | **22ms** | **438ms** | **0.045** |

22x faster than realtime. Piper returns `first_audio == total` on most lines,
meaning it synthesises a whole utterance before yielding — so sentence
splitting must happen in the daemon, not be delegated to Piper.

## 5. Two hardware realities

**Thermal throttling.** `package_throttle_count` reached 13,471 during these
runs. Back-to-back sweeps pushed small.en from 2165ms to 6283ms while WER held
constant. Sustained inference degrades this laptop 2-3x, so a long voice
session gets slower. Budget for it.

**Microphone gain.** ALSA `Capture` shipped at +12dB, clipping 1.46% of samples
at a normal speaking distance. Accuracy survived it, but install-time gain
calibration belongs in the product. `record.py` now refuses to accept a clipped
clip silently.

## 6. Packaging

The whole stack imports into one process on Arch's system Python 3.14:
pacman's `python-vosk` alongside wheel-installed `faster-whisper` and
`piper-tts`, via a venv built with `--system-site-packages`. onnxruntime and
ctranslate2 both publish cp314 wheels. No AUR package is required for the
benchmark.

---

# Later findings (2026-09-22)

Same machine. These supersede parts of the section above.

## 7. The energy gate was destroying the wake word

openWakeWord builds embeddings over a sliding window of *contiguous*
audio. The daemon dropped every frame below the microphone gate before the
detector saw it, which fragments the phrase. Measured over twelve recordings
of the author saying "hey jarvis", one persistent detector, fed as the daemon
feeds it:

| gate | frames kept | detected |
|---|---|---|
| -36 dBFS | 30% | 9/12, two scoring ~0.07 |
| -44 dBFS | 33% | 11/12 |
| **none** | **100%** | **11/12, every peak 1.00** |

openWakeWord is now fed every frame. The gate still guards the Vosk grammar,
which genuinely needs it, and still decides when a turn has ended.

## 8. A grammar cannot reject a phonetic neighbour

Vosk's grammar must choose between the phrase and `[unk]`, so a near miss maps
onto the phrase at full confidence. Measured:

| grammar | attack | score |
|---|---|---|
| "hey claude" | "hey cloud" | **1.00** |
| "hey codex" | "hey kodak" | **1.00** |
| "hey pi" | "apple pie" | 0.89 |
| "hey hermes" | "hey herbie" | **1.00** |

No confidence threshold can fix this; there is nothing to threshold.
openWakeWord scores the same class of attack at 0.898 against 0.996 for the
real phrase, which *is* separable — at 0.90.

## 9. The personal verifier earns its place

Both detectors fed identical live audio, eleven utterances:

| | fired | mean peak |
|---|---|---|
| with verifier | 11/11 | 0.963 |
| base model | 11/11 | 0.987 |

Then twelve held-out other-speaker clips — six Piper voices at synthesis
parameters outside the training range:

| | accepted | mean peak |
|---|---|---|
| with verifier | **0/12** | 0.134 |
| base model | 8/12 | 0.718 |

So it costs 0.024 of headroom on the owner's voice and rejects every
other-speaker clip the base model would have let through. That is the whole
argument for openWakeWord's extra 154MB.

Caveat: the other speakers are synthetic, and the verifier was trained on
synthetic negatives from the same voices at different parameters. A real
stranger at the microphone is still untested.

## 10. Memory, and what it costs to hold

| component | RSS |
|---|---|
| whisper `tiny.en` | 323 MB (indifferent to `cpu_threads`) |
| openWakeWord + scipy + sklearn | 179 MB |
| Vosk model | 114 MB |
| Piper voice | 48 MB |

The daemon idles near 594 MB with only the models in use. Releasing a model
needs `malloc_trim(0)` as well as dropping the reference — glibc otherwise
keeps the arenas and RSS barely moves (Vosk: 67MB freed on the drop, 117MB
after the trim).

CPU is not a concern: openWakeWord inference is 2.97ms per 80ms frame, 3.7%
of one core, and the whole daemon idles at 5.3%. `vad_threshold` does not
reduce that — it runs Silero VAD *after* the predictions and adds ~22%.

## 11. Barge-in is a hardware problem, not a software one

One microphone about a foot from the speaker it is listening to, at the
machine's normal listening volume (70%). Two separate questions, opposite
answers.

**Does the detector false-fire on our own speech?** No, at any volume.

| playback volume | bleed median | bleed p90 | mic absmax | wake score |
|---|---|---|---|---|
| 30% | -45.6 | -44.0 | 1733 | 0.001 silent |
| 50% | -43.7 | -36.4 | 4982 | 0.001 silent |
| 70% | -37.3 | -27.3 | 16537 | 0.001 silent |
| 100% | -29.5 | -18.1 | 32733 | 0.000 silent |

Listening while talking is safe. (At 100% the microphone input clips —
absmax 32733 — so full volume is a bad idea for other reasons.)

**Can it hear you over our own speech?** No, and the reason is a level
problem, not a model problem. Our own playback reaches the microphone at
the same loudness as the user's voice:

| condition | bed level | SNR | fires | median peak |
|---|---|---|---|---|
| quiet room | — | — | 19/20 | 0.95 |
| over our own reply | -32.6 dBFS | +1.2 dB | 5/20 | 0.44 |
| AEC, converged | -44.8 dBFS | +13.4 dB | 19/20 | 0.93 |

Levels are the 75th-percentile frame — the speech, not the pauses —
because SNR is the quantity that survives a change of speaker volume or
microphone gain, and both knobs move.

How much SNR the detector needs, sweeping a scaled bleed bed:

| SNR | +30 | +24 | +18 | +12 | +6 | 0 | -6 |
|---|---|---|---|---|---|---|---|
| fires /20 | 19 | 18 | 12 | 11 | 9 | 5 | 1 |

Reliable detection wants ~+24 dB. Reality at a normal volume is +1.2 dB,
a deficit of about 23 dB. A trained interrupt phrase would be swamped
identically — the failure is acoustic masking, not vocabulary — so the
hermes-satellite approach of training a second phrase does not transfer
to a machine with one microphone.

### SNR alone does not predict detection

The converged canceller scores 19/20 at +13.4 dB, while a *scaled* bleed
bed at +12 dB scores 11/20. Same SNR, very different outcome: what the
canceller leaves behind is decorrelated from the wake word and overlaps
its spectrum far less than speech-shaped bleed at equal level. Masking is
spectral, so any level-only rule of thumb here will mislead.

### The echo canceller works, with two caveats worth the space

`pactl load-module module-echo-cancel aec_method=webrtc` removes ~12 dB of
the active bleed level (median -37.3 to -45.9, p90 -27.3 to -43.7, absmax
16537 to 1296) and restores detection completely. Two things nearly made
this measurement a lie:

*The stock settings destroy the signal while looking like a triumph.*
webrtc's noise suppressor and AGC are on by default. They drop the median
bleed to -67 dBFS — an apparent 31 dB win — while clipping transients to
full scale (absmax 32768) and gating the wake word to **0/20**. Reporting
the median alone would have inverted the conclusion. Pass
`analog_gain_control=0 digital_gain_control=0 noise_suppression=0` and
leave the canceller alone to do its actual job.

*It converges slowly.* Over 45s of continuous far-end audio:

| elapsed | median | p90 |
|---|---|---|
| 0-9s | -44.7 | -29.0 |
| 9-18s | -47.0 | -41.1 |
| 18-27s | -47.7 | -46.3 |
| 36-45s | -49.1 | -46.8 |

The p90 improves ~18 dB but takes ~30 seconds. A spoken reply is five to
ten. Whether the filter stays converged across a session of many short
replies is untested, and is the open question that decides whether
acoustic barge-in is buildable here.

So: half duplex, with an explicit interrupt on a keybind. `agentvoice
interrupt` (SIGUSR2) cancels the speaker and the agent, and is a no-op
when the daemon is idle.

**Method note.** A first pass at this was wrong in two ways, both of the
same kind. The machine's volume had been turned down and later muted while
the measurements ran, so one "cancelled" bed was really silence and the
canceller appeared perfect; and levels were summarised as medians, which
is exactly what hid the full-scale clipping under AGC. Every number above
was re-measured at a known, verified volume with audio confirmed present,
and levels are reported as distributions. Detection figures come from
mixing recorded bleed into clean wake clips: that models the echo adding
linearly and does not exercise webrtc's double-talk suppressor, so the
converged AEC figure is an upper bound. Replaying wake clips through the
speaker instead was tried and rejected as a method — the round trip costs
so much fidelity that even a quiet room scores 3/6, leaving no headroom to
measure anything.

## 12. What each agent CLI can actually be asked to do

Version currency on 2026-09-22, against `mise latest`:

| CLI | installed | latest |
|---|---|---|
| claude | 2.1.278 | 2.1.278 |
| gemini | 0.60.0 | 0.60.0 |
| codex | 0.154.0 | 0.155.1 |
| agy (Antigravity) | — | 1.2.8 |

Nothing here is stale. The question is not age but capability, and it
differs enough per backend that one permission model cannot cover them.

### Verified by running the binaries

- **Claude Code** — PreToolUse hook via `--settings`, receives the pending
  call on stdin and blocks on the verdict. The only backend that can turn a
  spoken request into a question on screen. `--permission-prompt-tool`, the
  documented flag for exactly this, is accepted, loads the MCP server, and
  is never consulted.
- **Codex** — `codex exec --json`, four-line envelope, `thread_id` resumes
  and genuinely carries context. No interception point: withholding
  `--approve-for-me` leaves it at `approval: never`, `sandbox: read-only`.
- **Gemini** — one-shot, no session handle. Withdrawn for individual Code
  Assist accounts in June 2026; an AI Studio API key still drives the same
  binary, which is a paid, per-token arrangement rather than a plan.
- **Antigravity (`agy` 1.2.8)** — installed and its flags read off the
  binary, but not run: a turn needs a Google login. Every documented
  headless flag exists verbatim, except `--print-timeout`, which the docs
  give as 5 minutes and the binary as `0s`, meaning wait forever.

### Why Antigravity is the interesting one

It is the only backend whose own surface expresses what agentvoice's
permission levels mean, rather than collapsing them to safe-or-trusted:

    --mode plan | accept-edits     read-only, or edits without asking
    --add-dir <path>               scope the workspace to the project
    --sandbox                      terminal restrictions
    --dangerously-skip-permissions the trusted level, named honestly

Access matters more than features here. Antigravity CLI is included at
every tier including free, on a personal Google account, which is the
arrangement an Omarchy user is likely to have -- where Gemini now wants a
billed API key.

**It has PreToolUse hooks, and they cannot grant.** The decision vocabulary
is richer than Claude's (`allow`, `deny`, `ask`, `force_ask`,
`deny_unless_prior_grant`), but in headless mode `allow` is ignored: the
hook fires, returns valid JSON, raises no error, and the call is denied
anyway. Upstream issue #1053, open against 1.2.7. A hook there can restrict
and never permit, so it cannot be the thing that asks -- the same shape of
trap as `--permission-prompt-tool`, and worth re-testing before any adapter
depends on it.

## 13. `--add-dir` adds to a workspace; it does not restrict to one

Antigravity was given the `edits` level on the strength of its flags:
`--mode accept-edits` with `--add-dir <project>` reads exactly like "files
in the project change without asking". Tested on 2026-09-23 against agy
1.2.8, it is not:

    project:  /tmp/.../proj      --add-dir points here
    target:   /tmp/.../outside/target.txt
    prompt:   "write CHANGED into <target>, then say done"

    result:   status SUCCESS, "Done. Updated target.txt"
    target:   CHANGED

Repeated with `allowNonWorkspaceAccess: false` in
`~/.gemini/antigravity-cli/settings.json` -- the setting the docs describe
as restricting access outside project directories -- with the same result.
The workspace had also been trusted wholesale at login
(`trustedWorkspaces: ["/home/duaneo"]`), which is the likeliest reason, and
there is no per-invocation flag that restricts: `--sandbox` is terminal
restrictions, not file ones.

So the level was withdrawn. Our `edits` means *inside the project*, and the
same screen shows Claude Code honouring exactly that, so a user would read
across. A permission name that means one thing on one row and something
broader on the next is worse than having one fewer option.

Two locks, because a stored setting outlives the reasoning behind it: an
adapter only accepts a level it still declares, and the flag is chosen
defensively at the call site as well.

**What would bring it back:** a demonstration that writes outside the
project are refused. That is a measurement, not a reading of the flags --
the flags said it already.
