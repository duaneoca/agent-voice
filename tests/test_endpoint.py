"""The OpenAI-compatible endpoint, against a real HTTP server.

This is the one backend whose wire format never had to be guessed, so it is
also the one that can be tested properly: a server here speaks the documented
SSE dialect and the adapter is driven against it, rather than a fake standing
in for a protocol nobody has seen.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from adapters import load
from adapters.openai_compat import OpenAICompatible

RECEIVED: list[dict] = []
BEHAVIOUR = {"mode": "ok"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):        # keep the test output clean
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        RECEIVED.append({"body": json.loads(body or b"{}"),
                         "auth": self.headers.get("Authorization"),
                         "agent": self.headers.get("User-Agent")})
        if BEHAVIOUR["mode"] == "http_error":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"bad key"}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        pieces = ["A wake word ", "starts the conversation."]
        if BEHAVIOUR["mode"] == "garbage_mixed_in":
            self.wfile.write(b"data: not json\n\n")
            self.wfile.write(b": a comment line\n\n")
        for piece in pieces:
            event = {"choices": [{"delta": {"content": piece}}]}
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def server():
    RECEIVED.clear()
    BEHAVIOUR["mode"] = "ok"
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    httpd.shutdown()


class TestStreaming:
    def test_deltas_become_text(self, server):
        a = OpenAICompatible(base_url=server, model="m")
        text = "".join(c.text for c in a.send("hi") if c.text)
        assert text == "A wake word starts the conversation."

    def test_the_turn_ends(self, server):
        a = OpenAICompatible(base_url=server, model="m")
        assert any(c.done for c in a.send("hi"))

    def test_unparseable_events_are_stepped_over(self, server):
        BEHAVIOUR["mode"] = "garbage_mixed_in"
        a = OpenAICompatible(base_url=server, model="m")
        text = "".join(c.text for c in a.send("hi") if c.text)
        assert text == "A wake word starts the conversation."

    def test_an_http_error_is_reported_not_swallowed(self, server):
        BEHAVIOUR["mode"] = "http_error"
        a = OpenAICompatible(base_url=server, model="m")
        chunks = list(a.send("hi"))
        assert any(c.error and "401" in c.error for c in chunks)
        assert not any(c.text for c in chunks)


class TestConversation:
    def test_history_is_replayed_so_it_remembers(self, server):
        """There is no server-side session here, so continuity is ours to
        keep: the prior turns have to go back up the wire."""
        a = OpenAICompatible(base_url=server, model="m")
        # Drained, not peeked: the assistant turn is recorded at the end of
        # the stream, so a caller that walks away early never contributes it.
        sid = ""
        for c in a.send("remember 47"):
            if c.session_id:
                sid = c.session_id
        list(a.send("what number?", sid))
        sent = RECEIVED[-1]["body"]["messages"]
        assert any("remember 47" == m["content"] for m in sent)
        assert any(m["role"] == "assistant" for m in sent)

    def test_the_spoken_style_leads_the_conversation(self, server):
        a = OpenAICompatible(base_url=server, model="m")
        list(a.send("hi"))
        first = RECEIVED[-1]["body"]["messages"][0]
        assert first["role"] == "system" and "read aloud" in first["content"]


class TestKeyHandling:
    def test_a_key_is_sent_as_a_bearer_token(self, server):
        list(OpenAICompatible(base_url=server, model="m", api_key="sekrit").send("hi"))
        assert RECEIVED[-1]["auth"] == "Bearer sekrit"

    def test_no_key_sends_no_header(self, server):
        """Ollama and LM Studio need none, so absence must not be an error."""
        list(OpenAICompatible(base_url=server, model="m").send("hi"))
        assert RECEIVED[-1]["auth"] is None

    def test_the_key_is_not_reachable_from_the_adapter_name_or_posture(self, server):
        """Whatever the panel publishes must never carry it."""
        a = OpenAICompatible(base_url=server, model="m", api_key="sekrit")
        assert "sekrit" not in a.name
        assert "sekrit" not in a.posture("ask")


class TestSelection:
    def test_a_configured_endpoint_beats_the_desktop_agent(self):
        a = load("claude", endpoint={"url": "http://x/v1", "model": "m"})
        assert a is not None and a.name == "endpoint"

    def test_an_incomplete_endpoint_falls_through(self):
        """A URL with no model is not a configuration, it is a half-typed one."""
        a = load("claude", endpoint={"url": "http://x/v1", "model": ""})
        assert a is None or a.name == "claude"

    @pytest.mark.parametrize("url,local", [
        ("http://localhost:11434/v1", True),
        ("http://127.0.0.1:11434/v1", True),
        ("http://192.168.1.50:11434/v1", True),
        ("http://macmini.local:11434/v1", True),
        ("https://api.openai.com/v1", False),
        ("https://api.x.ai/v1", False),
    ])
    def test_local_and_remote_are_told_apart(self, url, local):
        """Same adapter, two very different promises about where the words go."""
        posture = OpenAICompatible(base_url=url, model="m").posture("ask")
        assert ("on your network" in posture) is local

    def test_no_permission_levels_are_offered(self):
        """It has no tools, so a level would govern nothing."""
        assert OpenAICompatible(base_url="http://x/v1", model="m").levels == ("ask",)


class TestAbandonedTurns:
    def test_a_turn_cut_short_is_not_remembered(self, server):
        """Interrupting drops the reply from the history, because the history
        is written when the stream ends and an interrupted stream never does.

        Recorded rather than fixed: the machine did not finish saying it, so
        leaving it out is defensible -- but it means a voice conversation can
        forget its own half of an exchange, and that should be a decision
        rather than a surprise.
        """
        a = OpenAICompatible(base_url=server, model="m")
        sid = ""
        for c in a.send("first"):
            if c.session_id:
                sid = c.session_id
                break                      # the shape of an interrupt
        list(a.send("second", sid))
        sent = RECEIVED[-1]["body"]["messages"]
        assert not any(m["role"] == "assistant" for m in sent)


class TestNoToolsIsNotTheSameAsRestrained:
    """An agent held read-only would act if allowed and refuses because it is
    not. A chat completion has nothing to refuse with. Saying "it may refuse
    work rather than ask" about the second describes a restraint that is not
    doing any work, and a project directory it never touches is noise."""

    def test_the_endpoint_declares_no_tools(self):
        assert OpenAICompatible(base_url="http://x/v1", model="m").has_tools is False

    def test_every_agent_backend_declares_tools(self):
        from adapters.antigravity import Antigravity
        from adapters.claude_code import ClaudeCode
        from adapters.codex import Codex
        from adapters.gemini import Gemini
        for cls in (ClaudeCode, Codex, Gemini, Antigravity):
            assert cls().has_tools is True, cls.__name__


class TestKeyFileShape:
    """Whatever shape the key file is in, the failure must not be a 401.

    The key is the one piece of configuration a user hand-writes into a file
    rather than a settings screen, so it arrives in whatever form the habit
    of the moment produces. Rejecting those costs an opaque authentication
    error; accepting them costs six lines.
    """

    @pytest.fixture
    def keyfile(self, tmp_path, monkeypatch):
        import importlib
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        for var in ("AGENTVOICE_ENDPOINT_KEY", "OPENAI_API_KEY", "XAI_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        import paths
        importlib.reload(paths)
        paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        yield paths
        importlib.reload(paths)

    @pytest.mark.parametrize("written", [
        "sk-abc123",
        "sk-abc123\n",
        "  sk-abc123  \n",
        "OPENAI_API_KEY=sk-abc123\n",
        "export OPENAI_API_KEY=sk-abc123\n",
        'OPENAI_API_KEY="sk-abc123"\n',
        "XAI_API_KEY='sk-abc123'\n",
        "# the key for openai\nsk-abc123\n",
    ])
    def test_every_plausible_shape_yields_the_key(self, keyfile, written):
        (keyfile.CONFIG_DIR / "endpoint.key").write_text(written)
        assert keyfile.endpoint_key() == "sk-abc123"

    def test_an_empty_file_is_no_key_not_a_crash(self, keyfile):
        (keyfile.CONFIG_DIR / "endpoint.key").write_text("\n\n# nothing\n")
        assert keyfile.endpoint_key() == ""

    def test_a_missing_file_is_no_key(self, keyfile):
        assert keyfile.endpoint_key() == ""

    def test_the_environment_wins_over_the_file(self, keyfile, monkeypatch):
        (keyfile.CONFIG_DIR / "endpoint.key").write_text("sk-from-file")
        monkeypatch.setenv("AGENTVOICE_ENDPOINT_KEY", "sk-from-env")
        assert keyfile.endpoint_key() == "sk-from-env"


class TestKeySources:
    """Where a key comes from, in order of how well the machine protects it.

    Omarchy has no key store to borrow: it installs each agent CLI and lets
    it handle its own authentication. So the order here is ours to choose,
    and it runs environment, then login keyring by host, then keyring
    generic, then a plaintext file for machines with no keyring.
    """

    @pytest.fixture
    def fresh(self, tmp_path, monkeypatch):
        import importlib
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        for var in ("AGENTVOICE_ENDPOINT_KEY", "OPENAI_API_KEY", "XAI_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        import paths
        importlib.reload(paths)
        paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        yield paths, monkeypatch
        importlib.reload(paths)

    def test_the_environment_beats_everything(self, fresh):
        paths, mp = fresh
        (paths.CONFIG_DIR / "endpoint.key").write_text("from-file")
        mp.setattr(paths, "_keyring", lambda **k: "from-keyring")
        mp.setenv("AGENTVOICE_ENDPOINT_KEY", "from-env")
        assert paths.endpoint_key("api.openai.com") == "from-env"

    def test_the_keyring_beats_the_file(self, fresh):
        paths, mp = fresh
        (paths.CONFIG_DIR / "endpoint.key").write_text("from-file")
        mp.setattr(paths, "_keyring", lambda **k: "from-keyring")
        assert paths.endpoint_key("api.openai.com") == "from-keyring"

    def test_the_file_is_the_fallback(self, fresh):
        paths, mp = fresh
        (paths.CONFIG_DIR / "endpoint.key").write_text("from-file")
        mp.setattr(paths, "_keyring", lambda **k: "")
        mp.setattr(paths, "_keyring_has_hosts", lambda: False)
        assert paths.endpoint_key("api.openai.com") == "from-file"

    def test_one_vendors_key_is_never_sent_to_another(self, fresh):
        """The leak this closes: with an OpenAI key in the file and the
        endpoint switched to xAI, the OpenAI credential would have gone to
        api.x.ai. Once anything is filed by host, a miss means no key."""
        paths, mp = fresh
        (paths.CONFIG_DIR / "endpoint.key").write_text("openai-key")
        mp.setattr(paths, "_keyring",
                   lambda **k: "openai-key" if k.get("endpoint") == "api.openai.com" else "")
        mp.setattr(paths, "_keyring_has_hosts", lambda: True)
        assert paths.endpoint_key("api.openai.com") == "openai-key"
        assert paths.endpoint_key("api.x.ai") == ""

    def test_a_missing_secret_tool_is_not_an_error(self, fresh):
        """Most machines have a keyring; a headless one may not."""
        paths, mp = fresh
        mp.setattr(paths.shutil, "which", lambda _: None)
        (paths.CONFIG_DIR / "endpoint.key").write_text("from-file")
        assert paths.endpoint_key("api.openai.com") == "from-file"


class TestStallAndInterrupt:
    """An endpoint that stops talking, and one the user stops.

    Both matter more here than for the subprocess backends: there is no
    process to kill, only a socket to stop reading from.
    """

    def test_a_silent_endpoint_gives_up_and_says_so(self):
        """The socket timeout is per-read, so it is already an idle timeout.

        A watchdog thread was tried first. It detected the stall correctly and
        could not end it -- closing an HTTPResponse from another thread does
        not interrupt a read blocked inside it, so the call still ran on to
        the server's own timeout, 60s in the test that caught this.
        """
        import threading
        import time
        # Threading, and a short stall: a plain HTTPServer handles requests
        # on the serve_forever thread, so shutdown() waits for the sleeping
        # handler and the whole suite pays for it.
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Silent(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.flush()
                time.sleep(5)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Silent)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            a = OpenAICompatible(
                base_url=f"http://127.0.0.1:{httpd.server_address[1]}/v1",
                model="m")
            a.idle_timeout_s = 1.0
            start = time.monotonic()
            errors = [c.error for c in a.send("hi") if c.error]
            elapsed = time.monotonic() - start
        finally:
            httpd.shutdown()

        assert elapsed < 10, f"waited {elapsed:.1f}s on a stalled endpoint"
        assert errors and "stopped responding" in errors[0]

    def test_cancel_is_not_reported_as_a_failure(self, server):
        """Interrupting closes the socket, and the read error that follows is
        the interrupt working -- not something to announce or speak."""
        a = OpenAICompatible(base_url=server, model="m")
        a.cancel()
        chunks = list(a.send("hi"))
        assert not any(c.error for c in chunks)


class TestUserAgent:
    def test_the_request_identifies_itself(self, server):
        """Cloudflare fronts several of these providers and blocks urllib's
        default agent outright: Groq answered 403 "error code 1010" to
        Python-urllib/3.13 and 200 to the same request from a named client.
        Every Groq model was unreachable until this was set.
        """
        list(OpenAICompatible(base_url=server, model="m").send("hi"))
        agent = RECEIVED[-1]["agent"]
        assert agent and "agentvoice" in agent
        assert "urllib" not in agent.lower()


class TestOverrideIsVisible:
    """An endpoint takes precedence over the desktop's agent.

    A reasonable rule and an unreasonable surprise: choosing Claude in
    Omarchy's settings and having nothing change is indistinguishable from
    the setting being broken. Found by the user doing exactly that.
    """

    def test_the_endpoint_still_wins(self):
        a = load("claude", endpoint={"url": "http://x/v1", "model": "m"})
        assert a is not None and a.name == "endpoint"

    def test_clearing_it_returns_to_the_desktop_agent(self):
        """Both boxes empty means follow omarchy default agent again."""
        a = load("claude", endpoint={"url": "", "model": ""})
        assert a is None or a.name == "claude"

    def test_a_half_filled_endpoint_does_not_override(self):
        """A URL with no model is a half-typed setting, not a choice."""
        for endpoint in ({"url": "http://x/v1", "model": ""},
                         {"url": "", "model": "m"}):
            a = load("claude", endpoint=endpoint)
            assert a is None or a.name == "claude", endpoint
