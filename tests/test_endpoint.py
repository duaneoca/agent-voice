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
                         "auth": self.headers.get("Authorization")})
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
