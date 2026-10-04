"""The contract every backend implements, and the guards around it."""
from __future__ import annotations

import stat
from pathlib import Path

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

    def test_the_hook_sees_every_tool(self):
        """The matcher is the limit of what the permission system can see, so
        naming the dangerous tools put the rest outside it: `mcp__*` from the
        user's own servers, WebSearch, Read and Task ran under
        `--permission-mode auto` while the screen said "asks before every
        change". Which tools are free is permission_hook.py's decision now.
        """
        import json
        import re
        settings = json.loads(ClaudeCode()._hook_settings())
        matcher = settings["hooks"]["PreToolUse"][0]["matcher"]
        for tool in ("Bash", "Write", "Edit", "WebSearch", "Task",
                     "mcp__mail__send", "Skill", "Read"):
            assert re.match(matcher, tool), f"{tool} would bypass the hook"

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
        # The four with their own modules have each answered a live turn;
        # nothing on the shared CLI template has.
        s = supported()
        for observed in ("claude", "codex", "gemini", "agy"):
            assert s[observed].startswith("observed"), (observed, s[observed])
        assert "UNVERIFIED" in s["grok"]
        for agent in CLI_TEMPLATES:
            assert not s[agent].startswith("observed"), agent


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
        """Point ~/.gemini/settings.json at a scratch file.

        Folder trust is switched off for this scratch home, because these
        tests are about auth: available() now answers both questions, and a
        scratch home trusts nothing, so without this every case below would
        fail for the other reason.
        """
        home = tmp_path / "home"
        (home / ".gemini").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("GEMINI_CLI_HOME", str(home))
        for var in ("GEMINI_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI",
                    "GOOGLE_GENAI_USE_GCA", "GEMINI_CLI_TRUST_WORKSPACE",
                    "GEMINI_RESTRICTED_MODE"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr("adapters.gemini.installed", lambda _: True)
        monkeypatch.setattr("adapters.gemini.folder_trust",
                            lambda _p: (True, "trusted in this test"))
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


class TestGeminiFolderTrust:
    """Whether a folder is trusted, resolved the way the CLI resolves it.

    Mirrored from gemini-cli 0.60.0 and checked against live runs: an
    untrusted folder makes `gemini -p` print its trust message and exit 55
    before the model is asked anything, so this decides whether a turn can
    happen at all.

    agentvoice used to set GEMINI_CLI_TRUST_WORKSPACE=true for whatever
    folder it was pointed at -- the same thing `--skip-trust` does -- which
    switched on exactly what trust withholds: the folder's own hooks, skills,
    project agents and stdio MCP servers. `projectDir` is an arbitrary
    directory, often a repository somebody else wrote.
    """

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        h = tmp_path / "home"
        (h / ".gemini").mkdir(parents=True)
        monkeypatch.setenv("GEMINI_CLI_HOME", str(h))
        for var in ("GEMINI_CLI_TRUST_WORKSPACE", "GEMINI_RESTRICTED_MODE"):
            monkeypatch.delenv(var, raising=False)
        return h

    @staticmethod
    def rules(home, mapping):
        import json as _json
        (home / ".gemini" / "trustedFolders.json").write_text(_json.dumps(mapping))

    def test_a_folder_with_no_rule_is_untrusted(self, home, tmp_path):
        """Not unknown: the CLI ends in `return isTrusted ?? false`."""
        from adapters.gemini import folder_trust
        assert folder_trust(str(tmp_path))[0] is False

    def test_a_trusted_folder_covers_everything_beneath_it(self, home, tmp_path):
        """Which is why this is answered once per project and not per boot."""
        from adapters.gemini import folder_trust
        project = tmp_path / "src" / "app"
        (project / "deep" / "deeper").mkdir(parents=True)
        self.rules(home, {str(project): "TRUST_FOLDER"})
        assert folder_trust(str(project))[0] is True
        assert folder_trust(str(project / "deep" / "deeper"))[0] is True
        assert folder_trust(str(tmp_path / "src"))[0] is False

    def test_trust_parent_trusts_the_parent(self, home, tmp_path):
        """So one answer can cover every sibling project in ~/src."""
        from adapters.gemini import folder_trust
        (tmp_path / "src" / "app").mkdir(parents=True)
        (tmp_path / "src" / "other").mkdir(parents=True)
        self.rules(home, {str(tmp_path / "src" / "app"): "TRUST_PARENT"})
        assert folder_trust(str(tmp_path / "src" / "other"))[0] is True

    def test_do_not_trust_on_an_ancestor_covers_the_whole_subtree(self, home, tmp_path):
        """The case on the development machine: one DO_NOT_TRUST line against
        the home directory distrusts every project under it."""
        from adapters.gemini import folder_trust
        (tmp_path / "src" / "app").mkdir(parents=True)
        self.rules(home, {str(tmp_path): "DO_NOT_TRUST"})
        trusted, why = folder_trust(str(tmp_path / "src" / "app"))
        assert trusted is False
        assert "DO_NOT_TRUST" in why

    def test_a_longer_rule_wins_over_a_shorter_one(self, home, tmp_path):
        """Longest prefix, so a trusted project inside a distrusted home
        still works. Verified against the live CLI."""
        from adapters.gemini import folder_trust
        project = tmp_path / "src" / "app"
        project.mkdir(parents=True)
        self.rules(home, {str(tmp_path): "DO_NOT_TRUST",
                          str(project): "TRUST_FOLDER"})
        assert folder_trust(str(project))[0] is True
        assert folder_trust(str(tmp_path))[0] is False

    def test_the_advice_for_a_refusal_it_can_escape_differs_from_one_it_cannot(
            self, home, tmp_path, monkeypatch):
        """Both refusals name the folder; only one of them is escaped by
        choosing a different project.

        It used to tell anyone whose home was untrusted to set a project
        folder in settings instead. With an explicit DO_NOT_TRUST on home
        that changes nothing -- the rule covers every folder inside it -- so
        the advice sent people somewhere that could not work. Verified
        against the live CLI: exit 55 in a covered subfolder, exit 0 once a
        longer TRUST_FOLDER rule is added.
        """
        from adapters.gemini import Gemini
        monkeypatch.setattr("adapters.gemini.installed", lambda _b: True)
        project = tmp_path / "src" / "app"
        project.mkdir(parents=True)

        self.rules(home, {str(tmp_path): "DO_NOT_TRUST"})
        said = Gemini(cwd=str(project), ask_permission=False).why_unavailable()
        assert "covers every folder inside it" in said, said
        assert "set a project folder" not in said, \
            "a different folder inherits the same refusal"

        # Nothing written about it at all: a trusted project folder is exactly
        # the fix, and that advice must survive.
        self.rules(home, {})
        monkeypatch.setattr("adapters.gemini.Path.home", classmethod(
            lambda _c: tmp_path))
        said = Gemini(cwd=str(tmp_path), ask_permission=False).why_unavailable()
        assert "set a project folder" in said, said

    def test_the_users_own_environment_variable_still_wins(self, home, tmp_path,
                                                           monkeypatch):
        """Ours to stop setting, theirs to set."""
        from adapters.gemini import folder_trust
        monkeypatch.setenv("GEMINI_CLI_TRUST_WORKSPACE", "true")
        assert folder_trust(str(tmp_path))[0] is True
        monkeypatch.setenv("GEMINI_CLI_TRUST_WORKSPACE", "false")
        assert folder_trust(str(tmp_path))[0] is False

    def test_restricted_mode_distrusts_everything(self, home, tmp_path, monkeypatch):
        from adapters.gemini import folder_trust
        self.rules(home, {str(tmp_path): "TRUST_FOLDER"})
        monkeypatch.setenv("GEMINI_RESTRICTED_MODE", "true")
        assert folder_trust(str(tmp_path))[0] is False

    def test_the_feature_being_off_trusts_everything(self, home, tmp_path):
        (home / ".gemini" / "settings.json").write_text(
            '{"security": {"folderTrust": {"enabled": false}}}')
        from adapters.gemini import folder_trust
        assert folder_trust(str(tmp_path))[0] is True

    def test_it_is_enabled_by_default(self, home, tmp_path):
        """0.60.0 defaults security.folderTrust.enabled to true, so absent
        settings must not read as absent trust."""
        from adapters.gemini import folder_trust
        assert folder_trust(str(tmp_path))[0] is False

    def test_a_corrupt_rules_file_is_not_trust(self, home, tmp_path):
        (home / ".gemini" / "trustedFolders.json").write_text("{not json")
        from adapters.gemini import folder_trust
        assert folder_trust(str(tmp_path))[0] is False


class TestGeminiHeadless:
    def test_the_trust_override_is_not_set_for_the_user(self, monkeypatch):
        """It is the CLI's `--skip-trust` by another name, and it switches on
        the folder's own hooks, skills, project agents and stdio MCP
        servers. That answer belongs to whoever owns the folder."""
        seen = {}

        class FakeProc:
            stdout, stderr = iter(()), None
            def wait(self): return 0

        def fake_popen(argv, **kw):
            seen.update(kw)
            return FakeProc()

        monkeypatch.setattr("adapters.gemini.subprocess.Popen", fake_popen)
        list(Gemini(ask_permission=True).send("hi"))
        assert "env" not in seen, seen.get("env")

    def test_an_untrusted_folder_is_reported_before_a_turn(self, tmp_path, monkeypatch):
        """Rather than as exit 55 afterwards, which tells nobody anything."""
        from adapters.gemini import NEVER_ASKED
        monkeypatch.setattr("adapters.gemini.installed", lambda _: True)
        # The real constant, not a paraphrase of it: the advice branches on
        # this exact string, and a stub that merely resembled it tested the
        # other branch while reading as though it tested this one.
        monkeypatch.setattr("adapters.gemini.folder_trust",
                            lambda _p: (False, NEVER_ASKED))
        agent = Gemini(cwd=str(tmp_path))
        assert agent.available() is False
        why = agent.why_unavailable()
        assert str(tmp_path) in why
        assert "gemini" in why and "once" in why

    def test_the_home_directory_gets_its_own_advice(self, monkeypatch):
        """Home cannot be trusted more specifically than itself, so "run
        gemini there once" is the wrong instruction for it.

        Only when nothing has been written about home, though: an explicit
        DO_NOT_TRUST there is inherited by the project folder too, and this
        test asserted the opposite until the live CLI said otherwise.
        """
        from adapters.gemini import NEVER_ASKED
        monkeypatch.setattr("adapters.gemini.installed", lambda _: True)
        monkeypatch.setattr("adapters.gemini.folder_trust",
                            lambda _p: (False, NEVER_ASKED))
        why = Gemini(cwd=str(Path.home())).why_unavailable()
        assert "project folder" in why

    def test_exit_55_is_reported_as_the_trust_problem(self, tmp_path, monkeypatch):
        class FakeProc:
            stdout, stderr = iter(()), None
            def wait(self): return 55

        monkeypatch.setattr("adapters.gemini.installed", lambda _: True)
        monkeypatch.setattr("adapters.gemini.folder_trust",
                            lambda _p: (False, "never asked"))
        monkeypatch.setattr("adapters.gemini.subprocess.Popen",
                            lambda argv, **kw: FakeProc())
        errors = [c.error for c in Gemini(cwd=str(tmp_path)).send("hi") if c.error]
        assert errors, "a refused turn must say something"
        assert "exited 55" not in errors[0], "the number is not an explanation"
        assert "trust" in errors[0]

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

    def test_the_answer_is_not_said_three_times(self, monkeypatch):
        """`item.started` and `item.updated` carry the same item again as it
        grows. Harvesting every item.* event spoke the answer once per
        event -- out loud, which is where that is unmissable."""
        growing = [
            self.OBSERVED[0], self.OBSERVED[1],
            '{"type": "item.started", "item": {"id": "item_0", "type": "agent_message",'
            ' "text": "A wake word"}}',
            '{"type": "item.updated", "item": {"id": "item_0", "type": "agent_message",'
            ' "text": "A wake word is a phrase"}}',
            self.OBSERVED[2],
            self.OBSERVED[3],
        ]
        text = "".join(c.text for c in self.replay(growing, monkeypatch) if c.text)
        assert text.count("A wake word") == 1, text

    def test_reasoning_is_not_spoken(self, monkeypatch):
        """It is the model thinking, not the answer."""
        with_thinking = [
            self.OBSERVED[0], self.OBSERVED[1],
            '{"type": "item.completed", "item": {"id": "r0", "type": "reasoning",'
            ' "text": "The user wants a definition, so I will keep it short."}}',
            self.OBSERVED[2], self.OBSERVED[3],
        ]
        text = "".join(c.text for c in self.replay(with_thinking, monkeypatch) if c.text)
        assert "The user wants" not in text
        assert "A wake word is a phrase" in text

    def test_a_turn_that_never_completes_is_still_answered(self, monkeypatch):
        """Skipping the growing events must not turn a working turn silent --
        that is the one failure a voice interface cannot report."""
        never_completes = [
            self.OBSERVED[0], self.OBSERVED[1],
            '{"type": "item.updated", "item": {"id": "item_0", "type": "agent_message",'
            ' "text": "A wake word is a phrase."}}',
            self.OBSERVED[3],
        ]
        text = "".join(c.text for c in self.replay(never_completes, monkeypatch) if c.text)
        assert "A wake word is a phrase." in text

    def test_a_tool_is_announced_once(self, monkeypatch):
        lines = [
            self.OBSERVED[0], self.OBSERVED[1],
            '{"type": "item.started", "item": {"type": "command_execution",'
            ' "name": "bash"}}',
            '{"type": "item.completed", "item": {"type": "command_execution",'
            ' "name": "bash"}}',
            self.OBSERVED[2], self.OBSERVED[3],
        ]
        tools = [c.tool for c in self.replay(lines, monkeypatch) if c.tool]
        assert tools == ["bash"], tools


class TestStderrIsDrained:
    """A pipe nobody reads is a child that stops writing.

    Every CLI adapter pipes stderr and reads it only after stdout ends. With
    more than a pipe-buffer of warnings on stderr, the child blocks on its
    next write, stops producing stdout, and the watchdog reports "stopped
    responding" for a turn that was waiting on us.
    """

    def test_a_noisy_child_still_delivers_its_output(self, tmp_path):
        import subprocess
        import sys

        from adapters.base import Drain

        noisy = tmp_path / "noisy.py"
        noisy.write_text(
            "import sys\n"
            "sys.stderr.write('x' * 500_000)\n"       # ~8 pipe buffers
            "sys.stderr.flush()\n"
            "print('the answer')\n"
        )
        proc = subprocess.Popen([sys.executable, str(noisy)],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
        errors = Drain(proc.stderr)
        out = list(proc.stdout)
        assert proc.wait(timeout=10) == 0
        assert out == ["the answer\n"], out
        assert errors.text()

    def test_what_it_collects_is_bounded(self, tmp_path):
        """A stack trace is worth keeping; a progress bar is not worth a
        hundred megabytes of it."""
        import subprocess
        import sys

        from adapters.base import Drain

        noisy = tmp_path / "flood.py"
        noisy.write_text("import sys\n"
                         "for _ in range(50_000): sys.stderr.write('y' * 100 + '\\n')\n")
        proc = subprocess.Popen([sys.executable, str(noisy)],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
        errors = Drain(proc.stderr, limit=1000)
        proc.wait(timeout=10)
        assert len(errors.text(timeout=5)) < 2000

    def test_every_cli_adapter_drains_it(self):
        """Named one by one, because the failure is a hang rather than an
        exception and nothing else would notice a new adapter without it."""
        import inspect

        from adapters.antigravity import Antigravity
        from adapters.claude_code import ClaudeCode
        from adapters.cli_agent import CliAgent
        from adapters.codex import Codex
        from adapters.gemini import Gemini

        for cls in (ClaudeCode, Codex, Gemini, Antigravity, CliAgent):
            source = inspect.getsource(cls.send)
            assert "stderr=subprocess.PIPE" in source, cls.__name__
            assert "Drain(proc.stderr)" in source, cls.__name__
            assert "proc.stderr.read()" not in source, cls.__name__


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
