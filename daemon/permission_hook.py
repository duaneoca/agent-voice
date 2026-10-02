#!/usr/bin/env python3
"""Ask the desktop before the agent runs anything.

Claude Code runs this as a PreToolUse hook: it receives the pending tool call
as JSON on stdin and answers with a permission decision. That is the supported
mechanism, and it is the one that works -- `--permission-prompt-tool` is
accepted by the CLI, loads the MCP server, and is then never consulted, at
least in 2.1.278.

The hook is spawned by Claude, not by the daemon, so the two talk through
files in the runtime directory: this writes <id>.request and waits for
<id>.response. Nothing polls that directory -- this process summons the
overlay itself and names the id, and the overlay reads the request file.

Silence means no. A request nobody answers is denied when the timeout expires,
because the alternative -- a spoken sentence quietly editing files while
nobody is at the screen -- is the thing this exists to prevent.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import CONFIG_DIR, RUNTIME_DIR, scope_root  # noqa: E402

PENDING = RUNTIME_DIR / "permissions"


def _timeout() -> float:
    """How long to wait for an answer. Garbage reads as the default.

    At import time, because the environment is read once -- but guarded,
    because an unparseable value raising here would abort the hook, and a
    hook that exits non-zero is a *non-blocking* error in Claude Code: the
    tool proceeds. Every failure in this file has to end in a denial.
    """
    try:
        value = float(os.environ.get("AGENTVOICE_PERMISSION_TIMEOUT", "45"))
    except (TypeError, ValueError):
        return 45.0
    return value if value >= 0 else 45.0


TIMEOUT = _timeout()

#: Tools with no effect outside the conversation. Asking about these would
#: train you to say yes, which is the failure mode a prompt has.
NO_EFFECT = {"TodoWrite", "BashOutput", "ExitPlanMode"}

#: Tools that only read. Allowed, but against SECRETS below rather than
#: unconditionally: the attack is not "the agent read a file", it is "a
#: sentence from a podcast told it to read ~/.ssh/id_ed25519 and put it
#: somewhere". A read it cannot act on is still a read it can quote.
READ_ONLY = {"Read", "NotebookRead", "Glob", "Grep"}

#: Everything else is asked about, including every `mcp__*` tool the user has
#: configured, WebSearch, WebFetch, Task and Skill. That is the point of the
#: `.*` matcher in adapters/claude_code.py: before it, those never reached
#: this process and ran under `--permission-mode auto` while the settings
#: screen said "asks before every change".

#: Paths whose contents are a credential, a key, or a history of what you
#: have typed. A read of one of these is asked about at every level below
#: "trusted", wherever it sits.
#:
#: A denylist, which is the weaker shape, and deliberately so: writes are
#: allowlisted to one project directory because an agent that cannot write
#: is useless, while a *read* of an ordinary file outside the project is
#: something an agent legitimately does all day -- a man page, a sibling
#: repository, a config it is being asked about. Prompting on all of those
#: would train the habit this file exists to avoid. So reads are open except
#: where the file is secret by nature. It will not be a complete list; it is
#: the list of things worth a question.
SECRETS = (
    "/.ssh/", "/.gnupg/", "/.aws/", "/.kube/", "/.netrc", "/.pgpass",
    "/.git-credentials", "/.docker/config.json", "/.config/gh/",
    "/.local/share/keyrings/", "/.password-store/", "/.mozilla/",
    "/.claude/.credentials.json", "/.codex/auth.json", "/.gemini/",
    "/.config/agentvoice/", "/.bash_history", "/.zsh_history",
    "/.python_history", "/.sh_history", "/shadow",
)

#: Same, by name rather than by location.
SECRET_NAMES = ("endpoint.key", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
                "credentials", "credentials.json", ".env")
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx")

#: Tools whose damage is confined to one named file, so a path rule can
#: describe them honestly. Bash is deliberately absent: "cd /; rm -rf ." has
#: no file_path, and no amount of prefix matching makes one safe to infer.
PATH_SCOPED = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

#: Paths that run code, or decide who may run code, on the next turn. A write
#: to one of these is never an "edit" in the sense the setting means, however
#: far inside the project it sits, so it is asked about even at "edits":
#:
#:   .claude/settings.json installs hooks that run unprompted;
#:   .mcp.json and .gemini/settings.json name commands the CLI will spawn;
#:   .git/hooks/* run on the next commit, .envrc on the next cd;
#:   .vscode/tasks.json runs on folder open;
#:   a .joblib is a pickle the daemon itself loads when its mtime changes.
#:
#: Matched as path fragments because the project is an arbitrary directory and
#: these sit at arbitrary depths inside it.
NEVER_SILENT = (
    "/.claude/", "/.mcp.json", "/.envrc", "/.git/hooks/", "/.vscode/tasks.json",
    "/.gemini/", "/.codex/", "/.config/omarchy/", "/.config/systemd/", "/.ssh/",
)

#: Same idea by extension rather than by location.
NEVER_SILENT_SUFFIX = (".joblib",)


def _sensitive(p: Path) -> bool:
    """True when a write to `p` has to be asked about whatever the level.

    "edits" is a statement about source files. These are not source files:
    each of them is a way to make something else run, including this
    daemon's own config and its own pickled verifier, so auto-allowing them
    would let one granted edit become arbitrary code on the next turn.
    """
    text = str(p)
    if any(frag in text + "/" for frag in NEVER_SILENT):
        return True
    if p.suffix in NEVER_SILENT_SUFFIX:
        return True
    try:
        return p == CONFIG_DIR or CONFIG_DIR in p.parents
    except (OSError, ValueError):
        return True


def _settings() -> tuple[str, Path | None]:
    """The permission level and project directory, read fresh.

    The hook is a separate process spawned by Claude, so it cannot be handed
    these by the daemon -- it has to read the same config the daemon reads.
    Any failure falls back to the strictest setting: a config this cannot
    parse must not become a reason to stop asking.

    The directory is None when no project is configured, which makes "edits"
    behave as "ask" rather than as "all of home". See `paths.scope_root`.
    """
    try:
        from runtime import Config
        cfg = Config()
        # argv[1] is the agent that installed this hook. Without it the level
        # cannot be resolved, and an unresolved level is "ask" -- a hook that
        # does not know who it is guarding must not stop guarding.
        agent = sys.argv[1] if len(sys.argv) > 1 else ""
        return cfg.level_for(agent), scope_root(cfg.get_str("projectDir"))
    except Exception:
        return "ask", None


def _inside(path: str, root: Path | None) -> bool:
    """True when `path` resolves to something under `root`.

    Resolved on both sides, because the whole point is to answer "is this in
    the project", and `~/project/../../etc/passwd` is not, however it is
    spelled.

    `resolve()` without `strict` is what does it: it resolves every symlink it
    can and leaves the rest of the path alone, so a file that does not exist
    yet is still judged -- Write creates files -- and so is a *dangling*
    symlink. Testing `exists()` first and resolving only the parent used to
    miss exactly that: `exists()` follows links, so a link whose target was
    not there yet counted as "does not exist", and `notes.txt ->
    ~/.config/systemd/user/evil.service` was judged to be inside the project.
    """
    if not path or root is None:
        return False
    try:
        p = Path(path).expanduser().resolve()
        return p == root or root in p.parents
    except (OSError, RuntimeError, ValueError):
        return False


def _resolved(path: str) -> Path:
    """`path` with symlinks and `..` taken out, or something harmless-looking
    that `_sensitive` will still judge rather than crash on."""
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return Path("/.ssh/unparseable")


def _paths_in(tool_input: dict) -> list[str]:
    """Every value in a tool's input that names a place on disk."""
    keys = ("file_path", "notebook_path", "path", "pattern", "glob")
    return [str(tool_input[k]) for k in keys
            if isinstance(tool_input.get(k), str) and tool_input[k]]


def _is_secret(path: str) -> bool:
    """True when reading `path` is worth a question however read-only it is."""
    p = _resolved(path)
    text = str(p) + "/"
    if any(frag in text for frag in SECRETS):
        return True
    if p.name in SECRET_NAMES or p.suffix in SECRET_SUFFIXES:
        return True
    return p.name.startswith(".env")


def decide(tool_name: str, tool_input: dict) -> tuple[str, str]:
    """Return (decision, reason). decision is 'allow' or 'deny'."""
    if tool_name in NO_EFFECT:
        return "allow", ""

    # Reads are free unless they are reads of a credential. Checked before
    # the level, because this holds at "ask" and at "edits" alike -- only
    # "trusted" below skips it, which is what "trusted" means.
    if tool_name in READ_ONLY:
        secrets = [p for p in _paths_in(tool_input) if _is_secret(p)]
        if not secrets:
            return "allow", ""
        level, _ = _settings()
        if level == "trusted":
            return "allow", ""
        return _ask(tool_name, tool_input,
                    note=f"this reads {secrets[0]}")

    level, root = _settings()
    if level == "trusted":
        return "allow", ""
    if level == "edits" and tool_name in PATH_SCOPED:
        target = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if _inside(target, root) and not _sensitive(_resolved(target)):
            return "allow", ""

    return _ask(tool_name, tool_input)


def _ask(tool_name: str, tool_input: dict, note: str = "") -> tuple[str, str]:
    """Put the question on screen and wait for an answer.

    Split out of decide() so both the write path and the read-a-secret
    path ask the same way, through the same file protocol and the same
    timeout. `note` is why it is being asked, when that is not obvious
    from the tool and the path.
    """
    # 0o700: the request holds whatever the agent is about to run, and the
    # response is the word "yes". Neither is anyone else's business, and a
    # response another local user could write would be a free allow.
    PENDING.mkdir(parents=True, exist_ok=True, mode=0o700)
    request_id = uuid.uuid4().hex[:12]
    request = PENDING / f"{request_id}.request"
    response = PENDING / f"{request_id}.response"

    # One line the overlay can show without wrapping. The full input goes
    # along too, for anything that wants the detail.
    summary = str(tool_input.get("command")
                  or tool_input.get("file_path")
                  or tool_input.get("path")
                  or tool_input.get("url")
                  or tool_input.get("pattern") or "")
    # Truncated, but never silently. `echo ok` followed by 400 spaces and
    # `; rm -rf ~/work` displayed as `echo ok`, which is a prompt that lies
    # about what it is asking for.
    shown, cut = summary[:400], max(0, len(summary) - 400)
    if cut:
        shown += f"… (+{cut} more characters, {summary.count(chr(10)) + 1} lines)"
    request.write_text(json.dumps({
        "id": request_id,
        "tool": tool_name,
        "summary": shown,
        "note": note,
        "truncated": cut,
        "length": len(summary),
        "input": tool_input,
        "asked_at": time.time(),
        "timeout": TIMEOUT,
    }))

    # Put it on screen. The overlay reads the request file itself, so the
    # payload only has to say which one. Failure here is not fatal: the
    # request still times out into a denial, which is the safe direction.
    try:
        import subprocess
        subprocess.run(["omarchy-shell", "shell", "summon", "duaneoca.agentvoice",
                        json.dumps({"mode": "permission", "id": request_id})],
                       timeout=5, capture_output=True)
    except Exception:
        pass

    deadline = time.time() + TIMEOUT
    try:
        while time.time() < deadline:
            if response.exists():
                try:
                    answer = json.loads(response.read_text())
                except Exception:
                    answer = {}
                if not isinstance(answer, dict):
                    answer = {}
                # `is True`, not truthiness: only the literal yes the overlay
                # writes counts. A response file holding anything else is a
                # response this cannot read, and that is a denial.
                if answer.get("allow") is True:
                    return "allow", ""
                return "deny", str(answer.get("reason") or "Denied at the desktop.")
            time.sleep(0.1)
        return "deny", f"Nobody answered within {TIMEOUT:.0f} seconds."
    finally:
        request.unlink(missing_ok=True)
        response.unlink(missing_ok=True)


def _die_cleanly() -> None:
    """Turn a kill into a normal unwind, so the request file goes away.

    Claude kills the whole process group when a turn is interrupted. The
    default SIGTERM disposition skips `finally`, which left the .request file
    behind -- and `agentvoice pending` then showed a question nobody was
    waiting for an answer to, forever.
    """
    def raise_it(signum, _frame):
        raise SystemExit(f"interrupted by signal {signum}")

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, raise_it)
        except (OSError, ValueError):
            pass


def _emit(decision: str, reason: str = "") -> int:
    out = {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
    }}
    if reason:
        out["hookSpecificOutput"]["permissionDecisionReason"] = f"agentvoice: {reason}"
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    return 0


def main() -> int:
    """Decide, and whatever happens, say so.

    Everything here is inside one catch, because of how Claude Code reads a
    hook: exit 0 with a decision is the decision, exit 2 is a block, and
    *anything else* -- an unhandled traceback, an ImportError, a full disk
    while writing the request file -- is a non-blocking error, which means
    the tool runs. So the failure mode of this file is not "the hook broke",
    it is "the guard was not there". Every path out of here is a denial that
    names itself.
    """
    _die_cleanly()
    try:
        try:
            payload = json.load(sys.stdin)
        except Exception:
            # An unreadable payload must not become an accidental allow.
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        tool = payload.get("tool_name") or "unknown"
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = {}

        decision, reason = decide(str(tool), tool_input)
        return _emit(decision, reason)
    except BaseException as e:                  # noqa: BLE001 -- see docstring
        try:
            return _emit("deny", f"the permission hook failed: "
                                 f"{type(e).__name__}. Nothing was run.")
        except BaseException:
            # Even the denial could not be written. Exit 2, which Claude Code
            # reads as a block in its own right.
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
