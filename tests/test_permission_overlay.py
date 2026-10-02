"""The overlay answers the question it was summoned about.

Two separate ways the card could lie about what it was asking. It read "the
newest pending request" rather than the id the hook named, so a second
question arriving first put one command on screen above the other's Allow
button. And closing it -- Esc, or a click outside -- wrote no verdict at all,
so the agent stayed blocked until the timeout with no way to reopen it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = (ROOT / "Settings.qml").read_text()


def _run(rundir: Path, *args: str) -> str:
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = str(rundir)
    done = subprocess.run([str(ROOT / "bin" / "agentvoice"), *args],
                          env=env, capture_output=True, text=True, timeout=30)
    return done.stdout.strip()


def _request(rundir: Path, request_id: str, command: str) -> None:
    d = rundir / "agentvoice" / "permissions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{request_id}.request").write_text(json.dumps(
        {"id": request_id, "tool": "Bash", "summary": command, "timeout": 45}))


class TestPendingById:
    def test_it_returns_the_request_that_was_asked_for(self, tmp_path):
        _request(tmp_path, "a" * 12, "rm -rf /")
        _request(tmp_path, "b" * 12, "ls")
        got = json.loads(_run(tmp_path, "pending", "b" * 12))
        assert got["summary"] == "ls"

    def test_an_unknown_id_is_not_some_other_request(self, tmp_path):
        """Falling back to "the newest" here would show one command and
        answer a different one."""
        _request(tmp_path, "a" * 12, "rm -rf /")
        assert json.loads(_run(tmp_path, "pending", "c" * 12)) == {}

    def test_a_malformed_id_is_refused_rather_than_used_as_a_path(self, tmp_path):
        _request(tmp_path, "a" * 12, "rm -rf /")
        assert json.loads(_run(tmp_path, "pending", "../../x")) == {}

    def test_with_no_id_it_still_answers_for_a_person_at_a_terminal(self, tmp_path):
        _request(tmp_path, "a" * 12, "rm -rf /")
        assert json.loads(_run(tmp_path, "pending"))["summary"] == "rm -rf /"

    def test_nothing_pending_says_so_rather_than_saying_nothing(self, tmp_path):
        """`set -euo pipefail` plus `ls` on a directory that is not there:
        ls exits 2, pipefail carries it out of the pipeline, and a failing
        command substitution in an assignment ends the script. So this
        printed nothing and exited 2, and the overlay only survived it by
        failing to parse the empty output. Same shape as the jq-exits-2 bug
        in install.sh."""
        env = dict(os.environ)
        env["XDG_RUNTIME_DIR"] = str(tmp_path)
        done = subprocess.run([str(ROOT / "bin" / "agentvoice"), "pending"],
                              env=env, capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout) == {}

    def test_an_empty_permissions_directory_is_also_nothing(self, tmp_path):
        (tmp_path / "agentvoice" / "permissions").mkdir(parents=True)
        assert json.loads(_run(tmp_path, "pending")) == {}

    def test_permit_refuses_an_id_that_is_not_one(self, tmp_path):
        """It is a filename: `../../x` would write a verdict anywhere."""
        env = dict(os.environ)
        env["XDG_RUNTIME_DIR"] = str(tmp_path)
        done = subprocess.run([str(ROOT / "bin" / "agentvoice"), "permit",
                               "../../x", "allow"], env=env,
                              capture_output=True, text=True, timeout=30)
        assert done.returncode == 2
        assert not list(tmp_path.rglob("*.response"))


class TestTheCardAnswers:
    def test_the_overlay_asks_for_the_summoned_id(self):
        body = SETTINGS[SETTINGS.index("function open("):]
        body = body[:body.index("\n  }")]
        assert "p.id" in body, "the payload names the request"
        assert '"pending", wanted' in body

    def test_closing_the_card_denies(self):
        body = SETTINGS[SETTINGS.index("function dismiss()"):]
        body = body[:body.index("\n  }")]
        assert 'answer("deny")' in body

    def test_a_verdict_is_written_only_once(self):
        """dismiss() is reached both by Esc and by answer() itself."""
        body = SETTINGS[SETTINGS.index("function dismiss()"):]
        body = body[:body.index("\n  }")]
        assert "!answered" in body
        assert "answered = true" in SETTINGS

    def test_the_countdown_and_the_request_are_reset_when_it_opens(self):
        body = SETTINGS[SETTINGS.index("function open("):]
        body = body[:body.index("\n  }")]
        assert re.search(r"secondsLeft\s*=\s*0", body)
        assert re.search(r"request\s*=\s*\(\{\}\)", body)
        assert re.search(r"answered\s*=\s*false", body)

    def test_the_command_is_shown_as_plain_text(self):
        """`ls <b></b><!-- ; curl x | sh -->` renders as `ls` under AutoText."""
        card = SETTINGS[SETTINGS.index('text: root.request.summary'):]
        card = card[:card.index("}")]
        assert "textFormat: Text.PlainText" in card
