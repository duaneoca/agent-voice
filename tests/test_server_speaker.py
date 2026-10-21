"""A read-aloud server as the voice, and Piper behind it.

ServerSpeaker cannot interleave `watch` with audio writes the way Speaker
does, because the server owns the speaker. So everything Speaker guarantees
by slicing -- a lock, a mute, the Stop button and barge-in all land within
one slice -- has to be guaranteed by polling instead, and these check that it
is. The server here is a real HTTP server on loopback with a scripted /now,
so the requests are the real ones and nothing needs a wheel.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class FakeServer:
    """Kokoro read-aloud's three endpoints, with /now driven by a script.

    `script` is a list of booleans consumed one per GET /now, after which
    `rest` is returned forever -- so [False, True, True] is "not started yet,
    then playing", and rest=False is "and then finished".
    """

    def __init__(self, script=(), rest=False, atoms=3, speak_status=200):
        self.script = list(script)
        self.rest = rest
        self.atoms = atoms
        self.speak_status = speak_status
        self.calls: list[tuple[str, str, object]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.calls.append(("GET", self.path, None))
                active = outer.script.pop(0) if outer.script else outer.rest
                self._reply(200, {"active": active})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.calls.append(("POST", self.path,
                                    (self.headers.get("Content-Type"), body)))
                if self.path == "/speak":
                    self._reply(outer.speak_status,
                                {"ok": True, "atoms": outer.atoms})
                else:
                    outer.rest = False
                    self._reply(200, {"ok": True})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def posts(self, path):
        return [c for c in self.calls if c[0] == "POST" and c[1] == path]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    made = []

    def make(**kw):
        s = FakeServer(**kw)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


class FakePiper:
    instances: list["FakePiper"] = []

    substituted_for = ""

    def __init__(self, voice):
        self.voice = voice
        self.name = self.requested = voice
        self.said = []
        self.watches = []
        FakePiper.instances.append(self)

    def say(self, text, watch=None):
        self.said.append(text)
        self.watches.append(watch)
        return 1.5

    def cancel(self):
        pass

    def superseded(self):
        return False


@pytest.fixture
def piper(monkeypatch):
    """Piper replaced, so a fallback is visible and needs no model."""
    import runtime
    FakePiper.instances = []
    monkeypatch.setattr(runtime, "Speaker", FakePiper)
    monkeypatch.setattr(runtime, "QUIET_MS", 100)
    monkeypatch.setattr(runtime, "POLL_MS", 10)
    return FakePiper.instances


def test_it_returns_only_once_the_server_has_gone_quiet(server, piper):
    """The microphone is deaf while we speak and opens when say() returns, so
    returning while the server is still talking means hearing ourselves."""
    import runtime
    s = server(script=[False, True, True, True, True], rest=False)

    runtime.ServerSpeaker(s.url).say("hello there.")

    gets = [c for c in s.calls if c[0] == "GET"]
    assert len(gets) >= 6, "returned before the server stopped reporting active"
    assert not piper, "a server that answered must not fall back to Piper"


def test_the_text_goes_as_json_because_the_server_refuses_anything_else(server, piper):
    """Kokoro read-aloud answers 415 to any POST that is not application/json,
    which is how it keeps web pages out."""
    import runtime
    s = server(script=[True], rest=False)

    runtime.ServerSpeaker(s.url).say("hello there.")

    (_, _, (ctype, body)), = s.posts("/speak")
    assert ctype == "application/json"
    assert body == {"text": "hello there."}


def test_a_gap_between_chunks_is_not_the_end_of_the_reply(server, piper, monkeypatch):
    """/now goes inactive briefly when synthesis falls behind playback. Ending
    the turn there would reopen the microphone over the next sentence."""
    import runtime
    monkeypatch.setattr(runtime, "QUIET_MS", 200)
    # Playing, a 50ms gap (5 polls of 10ms), playing again, then done.
    s = server(script=[True] * 3 + [False] * 5 + [True] * 3, rest=False)

    runtime.ServerSpeaker(s.url).say("one. two.")

    gets = [c for c in s.calls if c[0] == "GET"]
    assert len(gets) > 11, "ended in the gap instead of waiting out QUIET_MS"


def test_watch_stops_the_server_within_a_poll(server, piper):
    """A lock, a mute and barge-in all arrive through watch. Each has to reach
    the server as a /stop, or it keeps reading to a locked room."""
    import runtime
    s = server(rest=True)                       # would talk forever
    calls = {"n": 0}

    def watch():
        calls["n"] += 1
        return calls["n"] >= 3

    t0 = time.monotonic()
    runtime.ServerSpeaker(s.url).say("a long reply", watch=watch)

    assert time.monotonic() - t0 < 1.0
    assert s.posts("/stop"), "watch fired but the server was never told"


def test_cancel_from_another_thread_stops_the_server(server, piper):
    """The interrupt keybind and the Stop button call cancel() while say() is
    blocked in its poll on the daemon's thread."""
    import runtime
    s = server(rest=True)
    sp = runtime.ServerSpeaker(s.url)
    threading.Timer(0.1, sp.cancel).start()

    t0 = time.monotonic()
    sp.say("a long reply")

    assert time.monotonic() - t0 < 1.0
    assert s.posts("/stop")


def test_a_server_that_is_down_falls_back_to_piper(piper):
    """Being wrong out loud beats being silent: the reply is still spoken."""
    import runtime
    sp = runtime.ServerSpeaker("http://127.0.0.1:9", "amy-medium")

    def watch():
        return False

    assert sp.say("still heard", watch=watch) == 1.5
    assert piper and piper[0].voice == "amy-medium"
    assert piper[0].said == ["still heard"]
    assert piper[0].watches == [watch], "the fallback must keep the stop path"


def test_a_server_that_refuses_the_text_falls_back_to_piper(server, piper):
    import runtime
    s = server(speak_status=415)

    runtime.ServerSpeaker(s.url).say("refused")

    assert piper and piper[0].said == ["refused"]


def test_piper_is_loaded_once_however_long_the_server_is_down(piper):
    import runtime
    sp = runtime.ServerSpeaker("http://127.0.0.1:9")
    for _ in range(3):
        sp.say("again")
    assert len(piper) == 1
    assert piper[0].said == ["again"] * 3


def test_text_with_nothing_readable_returns_at_once(server, piper):
    """The server says so with atoms=0 and never starts, so waiting for it to
    start would hold the microphone shut for START_TIMEOUT_S."""
    import runtime
    s = server(atoms=0, rest=False)

    t0 = time.monotonic()
    assert runtime.ServerSpeaker(s.url).say("🙂") == 0.0
    assert time.monotonic() - t0 < 0.5


def test_a_server_that_never_starts_is_given_up_on(server, piper, monkeypatch):
    import runtime
    monkeypatch.setattr(runtime, "START_TIMEOUT_S", 0.2)
    s = server(rest=False)

    t0 = time.monotonic()
    runtime.ServerSpeaker(s.url).say("lost")

    assert time.monotonic() - t0 < 1.0
    assert s.posts("/stop"), "abandoned without making sure it stays quiet"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:5111", "http://localhost:5111", "http://[::1]:5111",
    "http://127.0.0.2:5111", "https://localhost:5111/",
])
def test_loopback_urls_are_accepted(url):
    import runtime
    assert runtime._loopback(url) == ""


@pytest.mark.parametrize("url", [
    "http://192.168.1.20:5111", "http://example.com:5111",
    "http://127.0.0.1.example.com", "ftp://127.0.0.1", "127.0.0.1:5111", "",
])
def test_anything_off_this_machine_is_refused(url):
    """Every reply is sent there as text, including file contents read out."""
    import runtime
    assert runtime._loopback(url) != ""


def test_a_refused_url_is_never_contacted_and_piper_speaks(server, piper):
    import runtime
    sp = runtime.ServerSpeaker("http://192.168.1.20:5111")

    sp.say("kept local")

    assert sp.refused
    assert piper and piper[0].said == ["kept local"]


def test_the_piper_voice_is_part_of_what_was_requested():
    """reconcile_voice compares `requested` against the settings. Leaving the
    voice out would mean changing it while a URL is set never reaches the
    fallback."""
    import runtime
    a = runtime.ServerSpeaker("http://127.0.0.1:5111", "amy-medium")
    b = runtime.ServerSpeaker("http://127.0.0.1:5111", "joe-medium")
    assert a.requested != b.requested


def test_reconcile_picks_the_server_when_a_url_is_set(daemon, cfg_factory, piper,
                                                    monkeypatch):
    import wake_listen
    monkeypatch.setattr(wake_listen, "Speaker", FakePiper)
    monkeypatch.setattr(daemon, "banner", lambda: None)   # needs a Pipeline
    daemon.cfg = cfg_factory(voiceUrl="http://127.0.0.1:5111")
    daemon.speaker = None

    daemon.reconcile_voice()

    assert isinstance(daemon.speaker, wake_listen.ServerSpeaker)
    first = daemon.speaker
    daemon.reconcile_voice()
    assert daemon.speaker is first, "rebuilt with nothing changed"

    daemon.cfg = cfg_factory(voiceUrl="")
    daemon.reconcile_voice()
    assert isinstance(daemon.speaker, FakePiper), \
        "clearing the URL must hand the voice back to Piper"
