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
import urllib.request
import uuid
from typing import Iterator

from .base import SPOKEN_STYLE, Adapter, Chunk

#: How many prior turns to replay. Voice conversations are short, and every
#: replayed turn is re-billed and re-read on the next request.
DEFAULT_HISTORY_TURNS = 8

#: Sent on every request. Not decoration: see the header block in send().
USER_AGENT = "agentvoice/0.2 (+https://github.com/duaneoca/agent-voice)"


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
            history = head + history[-(self.history_turns * 2):]
        self._sessions[sid] = history
        return sid, history

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
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body, headers=headers)

        reply = ""
        # urlopen's timeout is the *socket* timeout, applied to each blocking
        # read rather than to the request as a whole -- so it is already an
        # idle timeout, and a reliable one. A watchdog thread was tried first
        # and detected the stall correctly but could not end it: closing an
        # HTTPResponse from another thread does not interrupt a read blocked
        # inside it, so the call still ran to the server's own timeout.
        idle = min(self.timeout, self.idle_timeout_s)
        try:
            with urllib.request.urlopen(request, timeout=idle) as response:
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
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            yield Chunk(error=f"HTTP {e.code}: {detail}")
            return
        except TimeoutError:
            yield Chunk(error=f"{self.base_url} stopped responding after "
                              f"{idle:.0f}s")
            return
        except Exception as e:
            # Cancel stops this by closing the socket, so the resulting read
            # error is the interrupt working, not a fault worth announcing.
            if self._cancelled:
                yield Chunk(done=True, session_id=sid)
                return
            yield Chunk(error=f"{type(e).__name__}: {e}")
            return
        finally:
            self._response = None

        if reply:
            self._sessions[sid].append({"role": "assistant", "content": reply})
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
