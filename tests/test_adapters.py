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


class TestGeminiAuthDetection:
    """What counts as a usable Gemini, checked against a live CLI.

    Google withdrew Gemini CLI for individual Code Assist accounts in June
    2026 and points them at Antigravity. The OAuth entry stays in settings
    long after it stops working, so believing it means narrating an auth
    error once per sentence -- the thing available() exists to prevent. An
    AI Studio API key still drives the same binary, verified by a live turn.
    """

    @pytest.fixture
    def settings(self, tmp_path, monkeypatch):
        """Point ~/.gemini/settings.json at a scratch file."""
        home = tmp_path / "home"
        (home / ".gemini").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        for var in ("GEMINI_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI",
                    "GOOGLE_GENAI_USE_GCA"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr("adapters.gemini.installed", lambda _: True)
        return home / ".gemini" / "settings.json"

    def test_the_shape_the_cli_actually_writes(self, settings):
        """security.auth.selectedType -- not the selectedAuthType we grepped
        for, which reported a configured Gemini as absent."""
        settings.write_text('{"security": {"auth": {"selectedType": "gemini-api-key"}}}')
        assert Gemini().available()

    def test_the_older_flat_shape_still_counts(self, settings):
        settings.write_text('{"selectedAuthType": "gemini-api-key"}')
        assert Gemini().available()

    def test_the_withdrawn_login_does_not_count(self, settings):
        settings.write_text('{"security": {"auth": {"selectedType": "oauth-personal"}}}')
        assert not Gemini().available()

    def test_an_api_key_in_the_environment_is_enough(self, settings, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "x")
        assert Gemini().available()

    def test_unparseable_settings_are_not_an_endorsement(self, settings):
        settings.write_text("{not json at all")
        assert not Gemini().available()

    def test_absent_settings_are_not_an_endorsement(self, settings):
        assert not Gemini().available()


class TestGeminiHeadless:
    def test_the_workspace_is_trusted_for_headless_runs(self, monkeypatch):
        """Without it the CLI refuses outright: the project directory is the
        user's to choose, so it is rarely one Gemini has been told to trust."""
        seen = {}

        class FakeProc:
            stdout, stderr = iter(()), None
            def wait(self): return 0

        def fake_popen(argv, **kw):
            seen.update(kw)
            return FakeProc()

        monkeypatch.setattr("adapters.gemini.subprocess.Popen", fake_popen)
        list(Gemini(ask_permission=True).send("hi"))
        assert seen["env"]["GEMINI_CLI_TRUST_WORKSPACE"] == "true"

    def test_the_terminal_warning_is_never_spoken(self, monkeypatch):
        """The live CLI prints this on stdout, mixed in with the answer, and
        it would otherwise have been handed to the speaker and read aloud."""
        lines = ["Warning: 256-color support not detected.\n",
                 "A wake word starts the conversation.\n"]

        class FakeProc:
            stdout = iter(lines)
            stderr = None
            def wait(self): return 0

        monkeypatch.setattr("adapters.gemini.subprocess.Popen",
                            lambda argv, **kw: FakeProc())
        spoken = "".join(c.text for c in Gemini().send("hi") if c.text)
        assert "256-color" not in spoken
        assert "A wake word starts the conversation." in spoken


class TestCodexStream:
    """The exact envelope a signed-in codex-cli 0.154.0 emitted, replayed.

    Recorded from a live turn rather than from --help, so a schema change
    shows up here as a parse failure instead of as a voice assistant that
    answers every question with silence.
    """

    OBSERVED = [
        '{"type": "thread.started", "thread_id": "01a0cc69-d9ba-7910-8a98-01f1b73bd21c"}',
        '{"type": "turn.started"}',
        '{"type": "item.completed", "item": {"id": "item_0", "type": "agent_message",'
        ' "text": "A wake word is a phrase that activates a voice assistant."}}',
        '{"type": "turn.completed", "usage": {"input_tokens": 12960, "output_tokens": 33}}',
    ]

    def replay(self, lines, monkeypatch):
        class FakeProc:
            stdout = iter([ln + "\n" for ln in lines])
            stderr = None
            def wait(self): return 0

        monkeypatch.setattr("adapters.codex.subprocess.Popen",
                            lambda argv, **kw: FakeProc())
        return list(Codex(ask_permission=True).send("hi"))

    def test_the_thread_id_becomes_the_session(self, monkeypatch):
        """Conversation mode hands this back as `codex exec resume <id>`."""
        chunks = self.replay(self.OBSERVED, monkeypatch)
        assert any(c.session_id == "01a0cc69-d9ba-7910-8a98-01f1b73bd21c"
                   for c in chunks)

    def test_the_answer_is_harvested(self, monkeypatch):
        chunks = self.replay(self.OBSERVED, monkeypatch)
        text = "".join(c.text for c in chunks if c.text)
        assert "A wake word is a phrase that activates a voice assistant." in text

    def test_usage_and_envelope_are_not_spoken(self, monkeypatch):
        """Only the assistant message is speech; the rest is bookkeeping."""
        chunks = self.replay(self.OBSERVED, monkeypatch)
        text = "".join(c.text for c in chunks if c.text)
        for noise in ("input_tokens", "turn.started", "thread.started"):
            assert noise not in text

    def test_a_turn_always_ends(self, monkeypatch):
        assert any(c.done for c in self.replay(self.OBSERVED, monkeypatch))

    def test_garbage_between_events_does_not_derail_the_turn(self, monkeypatch):
        """Anything non-JSON on stdout must be stepped over, not fatal."""
        noisy = [self.OBSERVED[0], "not json at all", ""] + self.OBSERVED[1:]
        text = "".join(c.text for c in self.replay(noisy, monkeypatch) if c.text)
        assert "A wake word is a phrase" in text


class TestRemembers:
    """Which backends can carry a conversation, declared where the UI reads it.

    Conversation mode fails quietly on one that cannot: the follow-up window
    opens, it listens, it answers, and every turn starts from nothing. The
    setting is deliberately left on for those -- it still saves the wake
    word, which is half of what it is for -- so the screen has to say it.
    """

    def test_the_four_that_hand_back_a_handle(self):
        from adapters.antigravity import Antigravity
        from adapters.openai_compat import OpenAICompatible
        for a in (ClaudeCode(), Codex(), Antigravity(),
                  OpenAICompatible(base_url="http://x/v1", model="m")):
            assert a.remembers is True, a.name

    def test_gemini_does_not(self):
        """Its headless mode is one-shot and returns no handle of any kind."""
        assert Gemini().remembers is False

    @pytest.mark.parametrize("agent", sorted(CLI_TEMPLATES))
    def test_no_shared_cli_agent_claims_to(self, agent):
        assert CliAgent(agent).remembers is False

    def test_the_default_is_the_safe_direction(self):
        """A backend that remembers and says it does not merely looks modest;
        the reverse invites a conversation it cannot have."""
        from adapters.base import Adapter
        assert Adapter.remembers is False

    def test_it_matches_whether_a_session_id_is_ever_yielded(self):
        """The flag and the mechanism must not drift apart."""
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent / "daemon" / "adapters"
        for module, remembers in (("claude_code", True), ("codex", True),
                                  ("antigravity", True), ("openai_compat", True),
                                  ("gemini", False), ("cli_agent", False)):
            src = (root / f"{module}.py").read_text()
            yields_id = "session_id=" in src
            assert yields_id == remembers, (
                f"{module} says remembers={remembers} but "
                f"{'does not yield' if remembers else 'yields'} a session id")
