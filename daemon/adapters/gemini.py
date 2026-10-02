"""Gemini CLI, headless.

Deliberately uses plain text output rather than `-o stream-json`: guessing an
event schema produces an adapter that looks right and silently yields nothing,
which is the worst failure mode available. Plain stdout has no schema to get
wrong. The cost is losing tool-call visibility and token counts.

Watched against a live turn on 2026-09-22, which corrected three guesses:

  - auth lives at security.auth.selectedType, not the selectedAuthType this
    used to grep for, so a configured Gemini was reported as absent;
  - headless runs refuse outright in a folder the CLI has not been told to
    trust, and the project directory is the user's to choose, so it will
    usually be one Gemini has never seen;
  - the CLI prints "Warning: 256-color support not detected" on stdout, which
    this would have handed to the speaker and said out loud.

Google discontinued Gemini CLI for individual *Code Assist* accounts in June
2026 (IneligibleTierError, UNSUPPORTED_CLIENT) and points them at Antigravity.
An AI Studio API key still drives this CLI -- that is a different product from
the OAuth login that was withdrawn -- so the check below accepts a key and
refuses the login path rather than reporting a backend that 401s every turn.

Folder trust, as observed against gemini-cli 0.60.0 on 2026-10-01:

  - `security.folderTrust.enabled` defaults to **true**, and a folder with no
    rule is untrusted, not unknown: `isTrustedFolder()` ends in
    `return isTrusted ?? false`.
  - the decision lives in ~/.gemini/trustedFolders.json (0600) as
    path -> TRUST_FOLDER | TRUST_PARENT | DO_NOT_TRUST. It is permanent, not
    per session or per boot, and it is matched by *longest prefix* on
    symlink-resolved paths -- so trusting a directory once covers everything
    beneath it, and a DO_NOT_TRUST on an ancestor distrusts every project
    under it until a longer rule overrides it.
  - an untrusted headless run does not hang, which this used to say. It
    prints `Approval mode overridden to "default" because the current folder
    is not trusted`, then refuses with exit **55** and names the three ways
    out: `--skip-trust`, GEMINI_CLI_TRUST_WORKSPACE=true, or trusting the
    folder in interactive mode. There is no `gemini trust` subcommand in
    this version.
  - `--skip-trust` is not a weaker form of the environment variable: in the
    bundle it *is* `process.env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"`.

What trust gates is the repo's own ability to run things: project hooks,
workspace skills, project agents, stdio MCP servers, and MCP auto-approval.
So asserting it below is what lets an unfamiliar repo in projectDir load its
own `.gemini/settings.json` -- see the note at the call site.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Iterator

from .base import (
    Adapter, Chunk, Drain, SPOKEN_STYLE, Watchdog, installed, stop_tree,
)


#: What `gemini -p` exits with in a folder it does not trust. Observed, not
#: documented: 0.60.0 prints the trust message and exits 55 before the model
#: is asked anything.
TRUST_EXIT = 55


def _gemini_home() -> Path:
    """Where the CLI keeps its own state. GEMINI_CLI_HOME relocates it."""
    return Path(os.environ.get("GEMINI_CLI_HOME") or Path.home())


def _folder_trust_enabled() -> bool:
    """`security.folderTrust.enabled`, which defaults to true in 0.60.0."""
    try:
        data = json.loads((_gemini_home() / ".gemini/settings.json").read_text())
    except (OSError, ValueError):
        return True
    trust = ((data.get("security") or {}).get("folderTrust") or {})
    value = trust.get("enabled")
    return True if value is None else bool(value)


def folder_trust(path: str) -> tuple[bool, str]:
    """Is `path` a folder Gemini has been told to trust, and why.

    The same resolution the CLI does, read out of its own files rather than
    guessed, because the answer decides whether a turn can run at all: an
    untrusted folder makes `gemini -p` exit 55 without asking the model
    anything. Mirrored here so the panel can say so before a turn instead of
    reporting exit 55 afterwards.

    Checked against gemini-cli 0.60.0's `checkPathTrust`/`isPathTrusted`:

      - GEMINI_RESTRICTED_MODE=true, or TRUST_WORKSPACE=false, wins and
        distrusts; TRUST_WORKSPACE=true wins and trusts. Both are the user's
        own environment, not ours -- agentvoice used to set the second one
        itself for every folder, which handed an unfamiliar repo its own
        hooks, skills, project agents and stdio MCP servers on the first
        turn. That is exactly what folder trust is for, so it is theirs to
        answer now.
      - with the feature disabled in settings, everything is trusted.
      - otherwise ~/.gemini/trustedFolders.json decides, by *longest prefix*
        over real (symlink-resolved) paths. TRUST_PARENT means the rule's
        parent directory. A DO_NOT_TRUST on an ancestor therefore distrusts
        every project beneath it until a longer rule overrides it.
      - no rule at all is untrusted, not unknown: the CLI ends in
        `return isTrusted ?? false`.
    """
    if (os.environ.get("GEMINI_RESTRICTED_MODE") == "true"
            or os.environ.get("GEMINI_CLI_TRUST_WORKSPACE") == "false"):
        return False, "GEMINI_RESTRICTED_MODE is set"
    if os.environ.get("GEMINI_CLI_TRUST_WORKSPACE") == "true":
        return True, "GEMINI_CLI_TRUST_WORKSPACE=true is set in your environment"
    if not _folder_trust_enabled():
        return True, "folder trust is turned off in ~/.gemini/settings.json"

    try:
        rules = json.loads(
            (_gemini_home() / ".gemini/trustedFolders.json").read_text())
    except (OSError, ValueError):
        rules = {}
    if not isinstance(rules, dict):
        rules = {}

    def real(p: str) -> Path:
        try:
            return Path(p).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return Path(p)

    here = real(path)
    best_len, best_level, best_rule = -1, None, ""
    for rule, level in rules.items():
        if not isinstance(rule, str) or not isinstance(level, str):
            continue
        effective = real(str(Path(rule).parent)
                         if level == "TRUST_PARENT" else rule)
        if here == effective or effective in here.parents:
            if len(rule) > best_len:
                best_len, best_level, best_rule = len(rule), level, rule
    if best_level in ("TRUST_FOLDER", "TRUST_PARENT"):
        return True, f"trusted by the rule for {best_rule}"
    if best_level == "DO_NOT_TRUST":
        return False, f"{best_rule} is marked DO_NOT_TRUST, and that covers this folder"
    return False, "Gemini has never been asked about this folder"


class Gemini(Adapter):
    name = "gemini"

    def __init__(self, model: str | None = None, spoken: bool = True,
                 cwd: str | None = None, approval_mode: str | None = None,
                 ask_permission: bool = True):
        self.model = model
        self.spoken = spoken
        self.cwd = cwd
        # There is no way to prompt from here, so asking for permission means
        # choosing the mode that cannot act: `plan` is Gemini's documented
        # read-only mode. `yolo` is the unattended one and is only used when
        # the user has explicitly turned the guard off.
        self.approval_mode = approval_mode or ("plan" if ask_permission else "yolo")
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    #: Withdrawn for individuals in June 2026. Present in settings long after
    #: it stopped working, so treating it as usable means narrating an auth
    #: error once per sentence -- exactly what available() exists to prevent.
    DEAD_AUTH = {"oauth-personal", "LOGIN_WITH_GOOGLE"}

    def available(self) -> bool:
        if not installed("gemini"):
            return False
        # An untrusted folder is the same kind of fact as a missing login: the
        # turn cannot run, and it fails with exit 55 before the model is asked
        # anything. Reported as unavailable so the panel says why *before* a
        # turn rather than narrating a number afterwards.
        if not self._trusted():
            return False
        # An unauthenticated Gemini fails on every turn, so treat it as absent
        # rather than as a backend that errors once per sentence.
        if any(os.environ.get(k) for k in
               ("GEMINI_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_GCA")):
            return True
        settings = os.path.expanduser("~/.gemini/settings.json")
        try:
            with open(settings) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return False
        auth = ((data.get("security") or {}).get("auth") or {})
        chosen = auth.get("selectedType") or data.get("selectedAuthType")
        return bool(chosen) and chosen not in self.DEAD_AUTH

    def _trusted(self) -> bool:
        return folder_trust(self.cwd or os.getcwd())[0]

    def why_unavailable(self) -> str:
        if not installed("gemini"):
            return "gemini is not installed"
        trusted, why = folder_trust(self.cwd or os.getcwd())
        if not trusted:
            where = self.cwd or os.getcwd()
            home = str(Path.home())
            # Named in full, because the fix is per folder and permanent: one
            # interactive run in that directory writes the answer to
            # ~/.gemini/trustedFolders.json and it is never asked again.
            # Home is the one folder that cannot be trusted more specifically
            # than itself, so it gets its own sentence.
            if where == home:
                return (f"gemini does not trust {where} ({why}) — and that is "
                        f"your home directory, so set a project folder in "
                        f"Agent Voice settings instead")
            return (f"gemini does not trust {where} ({why}) — run `gemini` in "
                    f"that folder once and say yes; it is remembered")
        settings = os.path.expanduser("~/.gemini/settings.json")
        try:
            with open(settings) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return "gemini has no auth configured — run: gemini"
        auth = ((data.get("security") or {}).get("auth") or {})
        chosen = auth.get("selectedType") or data.get("selectedAuthType")
        if chosen in self.DEAD_AUTH:
            # The single most likely reason today, and the one nobody would
            # guess from a silent fallback to echoing.
            return ("Google withdrew this Gemini login in June 2026 — "
                    "use an API key, or switch to Antigravity")
        return "gemini has no auth configured — run: gemini"

    def send(self, text: str, session_id: str | None = None) -> Iterator[Chunk]:
        prompt = f"{SPOKEN_STYLE}\n\n{text}" if self.spoken else text
        argv = ["gemini", "-p", prompt, "--approval-mode", self.approval_mode]
        if self.model:
            argv += ["-m", self.model]

        # No trust override. This used to set GEMINI_CLI_TRUST_WORKSPACE=true
        # for whatever folder it was pointed at -- which is the same thing the
        # CLI's own `--skip-trust` does, and it is offered for headless use,
        # but it switches on precisely what folder trust exists to withhold:
        # the folder's own hooks, skills, project agents and stdio MCP
        # servers. `projectDir` is an arbitrary directory, often a repository
        # somebody else wrote, so that answer is the user's to give. An
        # untrusted folder is reported by why_unavailable() before a turn
        # starts, and by the exit-55 branch below if one starts anyway.
        try:
            proc = subprocess.Popen(argv, cwd=self.cwd, stdin=subprocess.DEVNULL, start_new_session=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1)
        except OSError as e:
            yield Chunk(error=f"could not start gemini: {e}")
            return

        # Drained in the background from here on: stderr is a 64KB pipe
        # and it is not read until stdout ends, so a chatty CLI fills it,
        # blocks on its next write, and stops producing stdout -- which the
        # watchdog then reports as "stopped responding".
        errors = Drain(proc.stderr)

        with self._lock:
            self._proc = proc
        dog = Watchdog(proc, self.idle_timeout_s)

        got_text = False
        try:
            # Gemini prints prose, and prefixes its own notices. Those are
            # dropped rather than spoken.
            for line in proc.stdout:
                dog.poke()
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith(("YOLO mode", "Approval mode", "Loaded cached",
                                        "Data collection", "Flushing", "Warning:",
                                        "Warning ")):
                    continue
                got_text = True
                yield Chunk(text=line)
            code = proc.wait()
            if dog.fired:
                # Killed for going quiet. Indistinguishable from a cancel by
                # exit code alone, and silence is the one thing a voice
                # interface must never answer with.
                yield Chunk(error=f"gemini stopped responding after "
                                  f"{dog.idle_s:.0f}s")
                return
            if code == TRUST_EXIT and not got_text:
                # The folder became untrusted between the availability check
                # and the turn, or the check was wrong. Either way the number
                # means one thing, and saying "gemini exited 55" out loud
                # tells nobody anything.
                yield Chunk(error=self.why_unavailable())
                return
            if code != 0 and not got_text and code not in (-15, 143, -9, 137):
                err = errors.text().splitlines()
                yield Chunk(error=(err[-1] if err else f"gemini exited {code}"))
        finally:
            dog.stop()
            with self._lock:
                self._proc = None
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except Exception:
                    pass

        # Gemini's headless mode is one-shot: no session handle to carry, so
        # each turn starts clean. Continuity would need the OpenAI-compatible
        # adapter against a Gemini endpoint instead.
        yield Chunk(done=True)

    def cancel(self) -> None:
        with self._lock:
            proc = self._proc
        if proc and proc.poll() is None:
            stop_tree(proc, signal.SIGTERM)
