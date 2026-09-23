#!/usr/bin/env python3
"""Ask the desktop before the agent runs anything.

Claude Code runs this as a PreToolUse hook: it receives the pending tool call
as JSON on stdin and answers with a permission decision. That is the supported
mechanism, and it is the one that works -- `--permission-prompt-tool` is
accepted by the CLI, loads the MCP server, and is then never consulted, at
least in 2.1.278.

The hook is spawned by Claude, not by the daemon, so the two talk through
files in the runtime directory: this writes <id>.request and waits for
<id>.response. The daemon watches that directory and puts the question on
screen.

Silence means no. A request nobody answers is denied when the timeout expires,
because the alternative -- a spoken sentence quietly editing files while
nobody is at the screen -- is the thing this exists to prevent.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import RUNTIME_DIR, project_dir  # noqa: E402

PENDING = RUNTIME_DIR / "permissions"
TIMEOUT = float(os.environ.get("AGENTVOICE_PERMISSION_TIMEOUT", "45"))

#: Read-only tools. Asking about these would train you to say yes.
ALWAYS_ALLOW = {"Read", "Glob", "Grep", "NotebookRead", "TodoWrite",
                "WebSearch", "Task", "BashOutput"}

#: Tools whose damage is confined to one named file, so a path rule can
#: describe them honestly. Bash is deliberately absent: "cd /; rm -rf ." has
#: no file_path, and no amount of prefix matching makes one safe to infer.
PATH_SCOPED = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _settings() -> tuple[str, Path]:
    """The permission level and project directory, read fresh.

    The hook is a separate process spawned by Claude, so it cannot be handed
    these by the daemon -- it has to read the same config the daemon reads.
    Any failure falls back to the strictest setting: a config this cannot
    parse must not become a reason to stop asking.
    """
    try:
        from runtime import Config
        cfg = Config()
        return cfg.str("permissionLevel"), project_dir(cfg.str("projectDir"))
    except Exception:
        return "ask", project_dir("")


def _inside(path: str, root: Path) -> bool:
    """True when `path` resolves to something under `root`.

    Resolved on both sides, because the whole point is to answer "is this in
    the project", and `~/project/../../etc/passwd` is not, however it is
    spelled. A path that does not exist yet is still judged -- Write creates
    files -- so the parent is what gets resolved.
    """
    if not path:
        return False
    try:
        p = Path(path).expanduser()
        p = p.resolve() if p.exists() else p.parent.resolve() / p.name
        return p == root or root in p.parents
    except (OSError, RuntimeError, ValueError):
        return False


def decide(tool_name: str, tool_input: dict) -> tuple[str, str]:
    """Return (decision, reason). decision is 'allow' or 'deny'."""
    if tool_name in ALWAYS_ALLOW:
        return "allow", ""

    level, root = _settings()
    if level == "trusted":
        return "allow", ""
    if level == "edits" and tool_name in PATH_SCOPED:
        target = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if _inside(target, root):
            return "allow", ""

    PENDING.mkdir(parents=True, exist_ok=True)
    request_id = uuid.uuid4().hex[:12]
    request = PENDING / f"{request_id}.request"
    response = PENDING / f"{request_id}.response"

    # One line the overlay can show without wrapping. The full input goes
    # along too, for anything that wants the detail.
    summary = (tool_input.get("command")
               or tool_input.get("file_path")
               or tool_input.get("path")
               or tool_input.get("url")
               or tool_input.get("pattern") or "")
    request.write_text(json.dumps({
        "id": request_id,
        "tool": tool_name,
        "summary": str(summary)[:400],
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
                if answer.get("allow"):
                    return "allow", ""
                return "deny", answer.get("reason") or "Denied at the desktop."
            time.sleep(0.1)
        return "deny", f"Nobody answered within {TIMEOUT:.0f} seconds."
    finally:
        request.unlink(missing_ok=True)
        response.unlink(missing_ok=True)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        # An unreadable payload must not become an accidental allow.
        payload = {}

    tool = payload.get("tool_name") or "unknown"
    decision, reason = decide(tool, payload.get("tool_input") or {})

    out = {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
    }}
    if reason:
        out["hookSpecificOutput"]["permissionDecisionReason"] = f"agentvoice: {reason}"
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
