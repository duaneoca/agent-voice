"""Any OpenAI-compatible /chat/completions endpoint, streamed.

This is the adapter that covers the most ground for the least guesswork: the
wire format is public and stable, so unlike the vendor CLIs there is nothing
here that had to be inferred. It serves Hermes (which is what
duaneoca/hermes-satellite talks to), Ollama, LM Studio, vLLM, OpenAI itself,
and xAI -- anything that speaks the same dialect.

Sessions are client-side: there is no server-side conversation to resume, so
the adapter keeps the transcript and replays it. That is the opposite of the
Claude Code adapter, where the CLI owns the session and we only keep its id.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Iterator

from .base import SPOKEN_STYLE, Adapter, Chunk

#: How many prior turns to replay. Voice conversations are short, and every
#: replayed turn is re-billed and re-read on the next request.
DEFAULT_HISTORY_TURNS = 8

#: How many conversations to remember at once. Sessions are client-side, so
#: nothing else ever frees them.
MAX_SESSIONS = 16

#: A spoken reply this long is not a reply. Streaming appends to a string in
#: memory, and the far end decides when to stop, so there has to be a point
#: at which this one does.
MAX_REPLY_CHARS = 100_000

#: Sent on every request. Not decoration: see the header block in send().
USER_AGENT = "agentvoice/0.2 (+https://github.com/duaneoca/agent-voice)"


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse redirects outright.

    urllib follows them by default and copies every header except the
    Content-* ones on the way -- `Authorization` included. So a 302 from the
    endpoint, or from anything sitting in front of a `http://` one, hands the
    bearer token to whatever host the Location names. There is no legitimate
    reason for /chat/completions to redirect, so the safe behaviour and the
    correct behaviour are the same: stop, and say why.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"refusing to follow a redirect to {newurl} with an API key "
            f"attached -- point the endpoint setting at the real URL",
            headers, fp)


#: One opener for every request, built once. Also the reason `urlopen` is not
#: used directly any more.
_OPENER = urllib.request.build_opener(_NoRedirects)


class OpenAICompatible(Adapter):
    name = "endpoint"
    remembers = True          # client-side: the transcript is replayed

    #: agentvoice offers this path no tools: it sends messages and reads text
    #: back, with no hook, no sandbox and nothing to gate. What sits behind
    #: the URL is another matter entirely, and not something that can be known
    #: from here -- the same adapter serves Ollama, which genuinely cannot
    #: touch anything, and Hermes, whose own documentation calls its bearer
    #: token equivalent to a root password. Asked for the hostname it was
    #: running on, Hermes answered with it.
    #:
    #: So the claim was withdrawn rather than guessed. `is_agent` is the
    #: user's statement about their own endpoint, and until they make it this
    #: says what it knows -- that agentvoice adds no tools -- and not what it
    #: cannot.
    levels = ("ask",)
    guards_permissions = False
    can_be_gated = False

    def __init__(self, base_url: str, model: str,
                 api_key: str | None = None,
                 spoken: bool = True,
                 is_agent: bool = False,
                 history_turns: int = DEFAULT_HISTORY_TURNS,
                 timeout: float = 120.0):
        self.is_agent = is_agent
        self.has_tools = is_agent
        if is_agent:
            # An agent goes quiet while it runs tools. Hermes sets its own
            # read timeout to 300s for exactly this reason and says so, so
            # the chat-endpoint limit would cut a working turn short.
            self.idle_timeout_s = 300.0
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.spoken = spoken
        self.history_turns = history_turns
        self.timeout = timeout
        # session id -> message list. Client-side, because this API has no
        # notion of a resumable conversation.
        self._sessions: dict[str, list[dict]] = {}
        self._cancelled = False
        # Held so cancel() can close it. Setting a flag is not enough: the
        # read blocks until the next token, so on a model with a long time to
        # first token an interrupt would not land for as long as that takes.
        self._response = None

    def available(self) -> bool:
        return bool(self.base_url and self.model)

    def why_unavailable(self) -> str:
        if not self.base_url:
            return "no endpoint URL set"
        return "no endpoint model set"

    def _is_local(self) -> bool:
        """True when the endpoint is on this machine or a private network.

        Worth distinguishing on screen rather than in documentation: the same
        adapter can be a model running in the next room or a transcript
        leaving for someone else's server, and those are not the same promise.
        """
        import ipaddress
        import urllib.parse

        host = (urllib.parse.urlparse(self.base_url).hostname or "").lower()
        if host in ("localhost", "::1") or host.endswith(".local"):
            return True
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        return address.is_loopback or address.is_private

    def _key_is_safe_to_send(self) -> bool:
        """False when the key would cross a network in the clear.

        `http://` to a box in the next room is how people actually run
        Ollama and Hermes, and gating that would be theatre -- but the same
        URL shape pointed at a public host puts a bearer token on the wire
        for anyone between here and there, and that token is sometimes, in
        Hermes' own words, equivalent to a root password.
        """
        scheme = (urllib.parse.urlparse(self.base_url).scheme or "").lower()
        return scheme == "https" or self._is_local()

    def posture(self, level: str) -> str:
        where = "on your network" if self._is_local() else "sent off this machine"
        if self.is_agent:
            import urllib.parse
            host = urllib.parse.urlparse(self.base_url).hostname or "the endpoint"
            return f"an agent — it can act on {host} · {where}"
        # Deliberately not "cannot read or change anything": that is a claim
        # about the far end, which this cannot see.
        return f"no tools from here · {where}"

    def _messages(self, text: str, session_id: str | None) -> tuple[str, list[dict]]:
        sid = session_id or str(uuid.uuid4())
        history = self._sessions.get(sid, [])
        if not history and self.spoken:
            history = [{"role": "system", "content": SPOKEN_STYLE}]
        history = history + [{"role": "user", "content": text}]
        # Keep the system message and the tail; the middle is what ages out.
        if len(history) > self.history_turns * 2 + 1:
            head = history[:1] if history[0]["role"] == "system" else []
            tail = history[-(self.history_turns * 2):]
            # A tail that begins with an assistant message is a transcript
            # that opens with an answer to a question that is no longer in
            # it. Drop it rather than send that.
            while tail and tail[0]["role"] == "assistant":
                tail.pop(0)
            history = head + tail
        self._sessions[sid] = history
        self._forget_old_sessions(sid)
        return sid, history

    def _forget_old_sessions(self, keep: str) -> None:
        """Bound the transcript store.

        One entry per conversation, each up to `history_turns` turns of text,
        and nothing ever removed one: a long-running daemon that had answered
        a few hundred turns was holding every one of them. Insertion order is
        age here, so the oldest goes first.
        """
        while len(self._sessions) > MAX_SESSIONS:
            oldest = next(iter(self._sessions))
            if oldest == keep:
                break
            del self._sessions[oldest]

    def _forget_turn(self, sid: str) -> None:
        """Take the prompt back out of the transcript after a failed turn.

        Otherwise the next turn appends a second `user` message with no
        `assistant` between them -- a shape some servers reject outright --
        and replays the prompt that failed as though it had been answered.
        """
        history = self._sessions.get(sid)
        if history and history[-1]["role"] == "user":
            history.pop()

    def send(self, text: str, session_id: str | None = None) -> Iterator[Chunk]:
        self._cancelled = False
        sid, messages = self._messages(text, session_id)
        yield Chunk(session_id=sid)

        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "stream": True,
        }).encode()
        # Cloudflare sits in front of several of these providers and blocks
        # urllib's default agent outright: Groq answers "403, error code 1010"
        # to Python-urllib/3.13 and 200 to anything that looks like a client
        # with a name. Identifying ourselves is both more honest and the only
        # way through.
        headers = {"Content-Type": "application/json",
                   "User-Agent": USER_AGENT}
        if self.api_key:
            if not self._key_is_safe_to_send():
                self._forget_turn(sid)
                yield Chunk(error=f"refusing to send an API key to "
                                  f"{self.base_url} over plain http. Use https, "
                                  f"or an endpoint on this machine.")
                return
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body, headers=headers)

        reply = ""
        # urlopen's timeout is the *socket* timeout, applied to each blocking
        # read rather than to the request as a whole -- so it is already an
        # idle timeout, and a reliable one. A watchdog thread was tried first
        # and detected the stall correctly but could not reliably end it:
        # closing an HTTPResponse from another thread interrupts a blocked
        # read on some transports and not on others -- against a TLS socket
        # the call ran on to the server's own timeout. cancel() still closes
        # the response, because where it does work it is immediate; the
        # socket timeout is what guarantees the turn ends either way.
        # The agent timeout is the longer of the two on purpose: an agent goes
        # quiet while it runs tools, and Hermes sets its own read timeout to
        # 300s for exactly that reason. `min` was the bug -- it picked the
        # 120s chat limit every time, so the longer limit never applied to
        # anything and the comment above __init__'s 300.0 was fiction.
        idle = max(self.timeout, self.idle_timeout_s) if self.is_agent \
            else min(self.timeout, self.idle_timeout_s)
        try:
            with _OPENER.open(request, timeout=idle) as response:
                self._response = response
                for raw in response:
                    if self._cancelled:
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    choices = event.get("choices") or [{}]
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        reply += delta
                        yield Chunk(text=delta)
                        if len(reply) > MAX_REPLY_CHARS:
                            break
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                detail = ""
            # A refused redirect has no body; its reason is the explanation.
            detail = detail.strip() or str(e.reason or "")
            self._forget_turn(sid)
            yield Chunk(error=f"HTTP {e.code}: {detail}")
            return
        except TimeoutError:
            self._forget_turn(sid)
            yield Chunk(error=f"{self.base_url} stopped responding after "
                              f"{idle:.0f}s")
            return
        except Exception as e:
            # Cancel stops this by closing the socket, so the resulting read
            # error is the interrupt working, not a fault worth announcing.
            if self._cancelled:
                if not reply:
                    self._forget_turn(sid)
                yield Chunk(done=True, session_id=sid)
                return
            self._forget_turn(sid)
            yield Chunk(error=f"{type(e).__name__}: {e}")
            return
        finally:
            self._response = None

        if reply:
            self._sessions[sid].append({"role": "assistant", "content": reply})
        else:
            self._forget_turn(sid)
        yield Chunk(done=True, session_id=sid)

    def cancel(self) -> None:
        self._cancelled = True
        # Close the socket so a read blocked waiting for the first token
        # returns now rather than whenever the model gets around to it.
        response, self._response = self._response, None
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
