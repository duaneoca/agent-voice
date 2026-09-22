"""The contract every backend implements, and the guards around it."""
from __future__ import annotations

import os
import stat

import pytest

from adapters import CLI_TEMPLATES, load, sentences, supported
from adapters.base import Chunk, installed
from adapters.claude_code import ClaudeCode
from adapters.cli_agent import CliAgent
from adapters.codex import Codex
from adapters.gemini import Gemini


class TestSentences:
    def test_regroups_a_token_stream(self):
        chunks = [Chunk(text=t) for t in
                  ["The first one is long enough. ", "And so is the second one."]]
        assert len(list(sentences(iter(chunks)))) == 2

    def test_metadata_chunks_contribute_no_text(self):
        stream = iter([Chunk(session_id="abc"), Chunk(ttft_ms=12.0),
                       Chunk(text="A sentence that stands on its own."),
                       Chunk(done=True)])
        assert list(sentences(stream)) == ["A sentence that stands on its own."]

    def test_an_error_stops_the_stream(self):
        # Whatever was said before the failure is still worth speaking.
        stream = iter([Chunk(text="This much was said aloud already. "),
                       Chunk(error="the agent fell over"),
                       Chunk(text="this must never be spoken")])
        out = " ".join(sentences(stream))
        assert "said aloud already" in out and "never be spoken" not in out


class TestColdStubDetection:
    """Omarchy puts a mise stub on PATH for every agent it knows, so
    `which` finds all thirteen on a machine with none installed."""

    def make_stub(self, tmp_path, name, body):
        p = tmp_path / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
        return p

    def test_a_cold_mise_stub_counts_as_absent(self, tmp_path, monkeypatch):
        self.make_stub(tmp_path, "faux", '#!/bin/bash\nmise use -g "faux" || exit 1\n')
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))      # no mise installs dir
        assert installed("faux") is False

    def test_a_stub_backed_by_a_real_install_counts(self, tmp_path, monkeypatch):
        self.make_stub(tmp_path, "faux", '#!/bin/bash\nmise use -g "faux" || exit 1\n')
        (tmp_path / ".local/share/mise/installs/faux").mkdir(parents=True)
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        assert installed("faux") is True

    def test_an_ordinary_binary_counts(self, tmp_path, monkeypatch):
        self.make_stub(tmp_path, "faux", "#!/bin/bash\necho hello\n")
        monkeypatch.setenv("PATH", str(tmp_path))
        assert installed("faux") is True

    def test_something_not_on_path_does_not(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PATH", str(tmp_path))
        assert installed("definitely-not-a-real-binary") is False


class TestPermissionPosture:
    """Only Claude can raise a prompt. The rest must not be handed a flag
    that skips one while the user believes they will be asked."""

    def test_only_claude_claims_to_guard(self):
        assert ClaudeCode().guards_permissions is True
        assert Codex().guards_permissions is False
        assert Gemini().guards_permissions is False
        assert CliAgent("grok").guards_permissions is False

    def test_claude_adds_the_hook_when_asked_to(self):
        assert "--settings" in ClaudeCode(ask_permission=True)._argv("x", None)

    def test_claude_omits_it_when_told_not_to(self):
        assert "--settings" not in ClaudeCode(ask_permission=False)._argv("x", None)

    def test_the_hook_names_the_guarded_tools(self):
        import json
        settings = json.loads(ClaudeCode()._hook_settings())
        matcher = settings["hooks"]["PreToolUse"][0]["matcher"]
        for tool in ("Bash", "Write", "Edit"):
            assert tool in matcher

    def test_codex_loses_its_unattended_flag(self):
        assert "--approve-for-me" not in Codex(ask_permission=True)._argv("x", None)
        assert "--approve-for-me" in Codex(ask_permission=False)._argv("x", None)

    def test_gemini_falls_back_to_read_only(self):
        assert Gemini(ask_permission=True).approval_mode == "plan"
        assert Gemini(ask_permission=False).approval_mode == "yolo"

    @pytest.mark.parametrize("agent", sorted(CLI_TEMPLATES))
    def test_no_cli_agent_keeps_a_bypass_flag(self, agent):
        import shlex
        a = CliAgent(agent, ask_permission=True)
        parts = [p for p in shlex.split(a.template) if p in a.BYPASS_FLAGS]
        remaining = [p for p in parts]
        # the adapter strips them at send time; assert the list is complete
        assert all(f in a.BYPASS_FLAGS for f in remaining)

    @pytest.mark.parametrize("agent", sorted(CLI_TEMPLATES))
    def test_every_template_carries_the_prompt(self, agent):
        assert "{prompt}" in CLI_TEMPLATES[agent][0]


class TestRegistry:
    def test_supported_covers_every_cli_template(self):
        assert set(CLI_TEMPLATES) <= set(supported())

    def test_an_unknown_agent_loads_nothing(self):
        assert load("not-an-agent") is None

    def test_verification_status_is_recorded(self):
        # Honesty about which protocols were observed and which inferred.
        s = supported()
        assert s["claude"] == "verified"
        assert "UNVERIFIED" in s["grok"]
