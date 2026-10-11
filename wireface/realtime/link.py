"""A connection to a realtime speech model: what ``RealtimeVoice`` needs from Gemini Live or OpenAI Realtime.

The model never hears the user (speech recognition runs on this PC and the words go to the agent): a link only adds
silent context, asks the model to speak, and brings its voice back. So the model says something only when the face
asks it to, never a reply of its own to what the user said. Everything comes back through the ``events`` object
(called on the event loop):

    on_ready()                    connected and configured
    on_response_start()           the model starts a spoken reply
    on_audio(pcm)                 a piece of it: 16-bit mono PCM at ``output_rate``
    on_transcript(text)           the words of it, as they come
    on_response_end(interrupted)  the reply is over (``interrupted``: cut off before the end)
    on_error(message)             something went wrong that the user may want to know about

Links are created per connection: when one ends (the service closes sessions after a while), ``RealtimeVoice``
makes a new one, resuming the conversation where the service allows it (``resume_handle``).
"""
from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod

import aiohttp

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 15


class LinkError(Exception):
    """The connection failed. ``fatal``: retrying won't help until the settings change (bad key, unknown model,
    no credit left)."""

    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


class Link(ABC):
    id = ""
    label = ""
    output_rate = 24000         # what the model's voice comes back as
    voices: tuple[str, ...] = ()
    default_model = ""
    default_voice = ""
    key_envs: tuple[str, ...] = ()    # where its API key is usually found

    def __init__(self, key: str, model: str, voice: str, instructions: str, events, resume_handle: str | None = None):
        self.key = key
        self.model = model or self.default_model
        self.voice = voice or self.default_voice
        self.instructions = instructions
        self.events = events
        self.resume_handle = resume_handle
        self.resumed = False                  # this connection carried on an earlier conversation
        self.responding = False               # a spoken reply is in progress
        self.closed_reason: str | None = None
        self._session: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self._send_lock = asyncio.Lock()

    # --- implemented by each service ---
    @abstractmethod
    async def connect(self) -> None:
        """Open the connection and configure the session; raises LinkError."""

    @abstractmethod
    async def handle(self, msg: dict) -> None:
        """One message from the service."""

    @abstractmethod
    async def add_context(self, text: str) -> None:
        """Something for the model to know, without a reply."""

    @abstractmethod
    async def respond(self, text: str) -> None:
        """Add ``text`` and have the model speak now."""

    @abstractmethod
    async def cancel(self) -> None:
        """Stop the reply in progress."""

    async def truncate(self, heard_ms: int) -> None:
        """The user heard only the first ``heard_ms`` of the reply that was cut off (so the model knows)."""

    # --- shared ---
    async def run(self) -> None:
        """Read until the connection ends (``closed_reason`` says why)."""
        try:
            while self.ws is not None:
                msg = await self.ws.receive()
                if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    try:
                        data = json.loads(msg.data)
                    except ValueError:
                        log.warning("%s: unreadable message", self.id)
                        continue
                    try:
                        await self.handle(data)
                    except Exception:
                        log.exception("%s: handling a message", self.id)
                    continue
                if msg.type == aiohttp.WSMsgType.ERROR:
                    self.closed_reason = self.closed_reason or f"connection error: {self.ws.exception()}"
                elif msg.type == aiohttp.WSMsgType.CLOSE:
                    self.closed_reason = self.closed_reason or f"{msg.extra or 'closed'} ({msg.data})"
                else:
                    self.closed_reason = self.closed_reason or "closed"
                break
        finally:
            if self.responding:
                self.responding = False
                self.events.on_response_end(True)

    async def send(self, payload: dict) -> None:
        ws = self.ws
        if ws is None or ws.closed:
            return
        async with self._send_lock:
            try:
                await ws.send_str(json.dumps(payload))
            except (ConnectionError, RuntimeError, aiohttp.ClientError) as e:
                log.debug("%s: send failed: %s", self.id, e)

    async def _open(self, url: str, **kw) -> None:
        self._session = aiohttp.ClientSession()
        try:
            self.ws = await self._session.ws_connect(url, max_msg_size=0, heartbeat=30,
                                                     timeout=aiohttp.ClientWSTimeout(ws_close=5), **kw)
        except aiohttp.WSServerHandshakeError as e:
            await self.close()
            raise LinkError(f"{self.label} refused the connection ({e.status})", fatal=e.status in (401, 403, 404)) from None
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
            await self.close()
            raise LinkError(f"can't reach {self.label}: {e or type(e).__name__}") from None

    async def close(self) -> None:
        ws, self.ws = self.ws, None
        if ws is not None and not ws.closed:
            try:
                await ws.close()
            except Exception:
                pass
        session, self._session = self._session, None
        if session is not None:
            await session.close()

