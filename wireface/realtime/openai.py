"""OpenAI Realtime: the realtime voice over OpenAI's Realtime API WebSocket.

The model's voice comes down as 24 kHz PCM with a transcript. Context is a ``system`` message added with
``conversation.item.create``; ``response.create`` makes it speak. No audio goes up and turn detection is off: the
model never hears the user, so it never answers them on its own. A session lasts at most an hour and cannot be
resumed: the face starts a new one and tells it the latest activity again.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging

from aiohttp import WSMsgType

from .link import CONNECT_TIMEOUT_S, Link, LinkError

log = logging.getLogger(__name__)

URL = "wss://api.openai.com/v1/realtime"
FATAL = {"invalid_api_key", "insufficient_quota", "credit_balance_exhausted", "model_not_found", "invalid_model",
         "account_deactivated", "billing_hard_limit_reached"}


class OpenAILink(Link):
    id = "openai"
    label = "OpenAI Realtime"
    output_rate = 24000
    default_model = "gpt-realtime-2.1"
    default_voice = "marin"
    voices = ("marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse")
    key_envs = ("OPENAI_API_KEY",)

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._item: str | None = None       # the assistant audio item being spoken (for truncate)

    def _session_update(self) -> dict:
        return {"type": "session.update", "session": {
            "type": "realtime",
            "instructions": self.instructions,
            "output_modalities": ["audio"],
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": 24000}, "turn_detection": None},
                "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": self.voice},
            },
        }}

    async def connect(self) -> None:
        await self._open(URL, params={"model": self.model}, headers={"Authorization": f"Bearer {self.key}"})
        await self.send(self._session_update())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONNECT_TIMEOUT_S
        while True:
            try:
                msg = await asyncio.wait_for(self.ws.receive(), max(0.1, deadline - loop.time()))
            except asyncio.TimeoutError:
                await self.close()
                raise LinkError(f"{self.label} did not answer") from None
            if msg.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
                reason = str(msg.extra or "") if msg.type == WSMsgType.CLOSE else ""
                await self.close()
                raise LinkError(f"{self.label}: {reason or 'the connection closed during setup'}")
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            kind = data.get("type")
            if kind == "session.updated":
                self.events.on_ready()
                return
            if kind == "error":
                err = data.get("error") or {}
                code = err.get("code") or err.get("type") or ""
                await self.close()
                raise LinkError(_explain(err), fatal=code in FATAL or err.get("type") in FATAL)

    async def handle(self, msg: dict) -> None:
        kind = msg.get("type", "")
        if kind == "response.created":
            if not self.responding:
                self.responding = True
                self._item = None
                self.events.on_response_start()
        elif kind == "response.output_audio.delta":
            self._item = msg.get("item_id") or self._item
            self.events.on_audio(base64.b64decode(msg.get("delta") or ""))
        elif kind == "response.output_audio_transcript.delta":
            if msg.get("delta"):
                self.events.on_transcript(msg["delta"])
        elif kind == "response.done":
            resp = msg.get("response") or {}
            status = resp.get("status")
            if status == "failed":
                err = (resp.get("status_details") or {}).get("error") or {}
                self.events.on_error(_explain(err) if err else f"{self.label}: the reply failed")
            if self.responding:
                self.responding = False
                self.events.on_response_end(status in ("cancelled", "incomplete", "failed"))
        elif kind == "error":
            err = msg.get("error") or {}
            if err.get("code") in ("response_cancel_not_active", "conversation_already_has_active_response"):
                log.debug("openai: %s", err.get("message"))
            else:
                log.warning("openai error: %s", err)
                self.events.on_error(_explain(err))

    async def add_context(self, text: str) -> None:
        await self.send({"type": "conversation.item.create",
                         "item": {"type": "message", "role": "system", "content": [{"type": "input_text", "text": text}]}})

    async def respond(self, text: str) -> None:
        await self.add_context(text)
        await self.send({"type": "response.create"})

    async def cancel(self) -> None:
        if self.responding:
            await self.send({"type": "response.cancel"})

    async def truncate(self, heard_ms: int) -> None:
        if self._item:
            await self.send({"type": "conversation.item.truncate", "item_id": self._item, "content_index": 0,
                             "audio_end_ms": max(0, int(heard_ms))})


def _explain(err: dict) -> str:
    code = err.get("code") or err.get("type") or ""
    message = err.get("message") or code or "unknown error"
    if code == "invalid_api_key":
        return "OpenAI refused the API key: check OPENAI_API_KEY or the key saved in Settings"
    if code in ("insufficient_quota", "credit_balance_exhausted", "billing_hard_limit_reached"):
        return "The OpenAI account has no credit left for the realtime voice"
    return f"OpenAI: {message}"
