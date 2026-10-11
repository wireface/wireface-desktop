"""Gemini Live (Google): the realtime voice over the Live API's WebSocket (BidiGenerateContent).

The model's voice comes down as 24 kHz PCM with a transcript of it. Context is added with ``clientContent`` and
``turnComplete: false`` (no reply); ``turnComplete: true`` makes it speak. No audio goes up: the model never hears
the user, so it never answers them on its own. Gemini has no "cancel": a reply that is cut off is simply not played (its remaining audio
is dropped until the turn ends). Sessions are resumable: the service hands out a handle, and a new connection that
presents it carries on the same conversation (it closes connections after about ten minutes, with a warning).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging

from aiohttp import WSMsgType

from .link import CONNECT_TIMEOUT_S, Link, LinkError

log = logging.getLogger(__name__)

URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"


class GeminiLink(Link):
    id = "gemini"
    label = "Gemini Live"
    output_rate = 24000
    default_model = "gemini-3.8-live"
    default_voice = "Puck"
    voices = ("Puck", "Charon", "Kore", "Fenrir", "Aoede", "Leda", "Orus", "Zephyr", "Callirrhoe", "Autonoe",
              "Enceladus", "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia",
              "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi", "Vindemiatrix",
              "Sadachbia", "Sadaltager", "Sulafat")
    key_envs = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.thinking_off = True          # lowest latency; dropped if the model doesn't take the setting
        self._dropping = False            # the reply in progress was cancelled: ignore the rest of it
        self.go_away = False              # the service said it will close this connection soon

    def _setup(self) -> dict:
        gen = {"responseModalities": ["AUDIO"],
               "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice}}}}
        if self.thinking_off:
            gen["thinkingConfig"] = {"thinkingBudget": 0}
        setup = {
            "model": self.model if self.model.startswith("models/") else f"models/{self.model}",
            "generationConfig": gen,
            "systemInstruction": {"parts": [{"text": self.instructions}]},
            "outputAudioTranscription": {},
            "contextWindowCompression": {"slidingWindow": {}},
            "sessionResumption": {"handle": self.resume_handle} if self.resume_handle else {},
        }
        return {"setup": setup}

    async def connect(self) -> None:
        for attempt in range(2):
            await self._open(URL, params={"key": self.key})
            await self.send(self._setup())
            try:
                msg = await asyncio.wait_for(self.ws.receive(), CONNECT_TIMEOUT_S)
            except asyncio.TimeoutError:
                await self.close()
                raise LinkError(f"{self.label} did not answer") from None
            data = None
            if msg.type in (WSMsgType.TEXT, WSMsgType.BINARY):
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    pass
            if isinstance(data, dict) and "setupComplete" in data:
                self.resumed = bool(self.resume_handle)
                self.events.on_ready()
                return
            closing = msg.type == WSMsgType.CLOSE
            reason = str(msg.extra or "").strip() if closing else ""
            code = msg.data if closing else None
            await self.close()
            if attempt == 0 and self.thinking_off and "thinking" in reason.lower():
                self.thinking_off = False
                continue
            if attempt == 0 and self.resume_handle and "resum" in reason.lower():
                self.resume_handle = None             # the old conversation can't be picked up: start a new one
                continue
            if code in (1007, 1008):
                raise LinkError(_explain(reason), fatal=True)
            raise LinkError(f"{self.label}: {reason or 'the connection closed during setup'}")

    async def handle(self, msg: dict) -> None:
        if "sessionResumptionUpdate" in msg:
            upd = msg["sessionResumptionUpdate"]
            if upd.get("resumable") and upd.get("newHandle"):
                self.resume_handle = upd["newHandle"]
        if "goAway" in msg:
            self.go_away = True
            log.info("gemini: the service will close this connection (%s left)", msg["goAway"].get("timeLeft"))
        sc = msg.get("serverContent")
        if not sc:
            return
        parts = sc.get("modelTurn", {}).get("parts") or []
        said = sc.get("outputTranscription", {}).get("text")
        if (parts or said) and not self.responding:
            self.responding = True
            self._dropping = False
            self.events.on_response_start()
        if not self._dropping:
            for part in parts:
                blob = part.get("inlineData")
                if blob and str(blob.get("mimeType", "")).startswith("audio/pcm"):
                    self.events.on_audio(base64.b64decode(blob["data"]))
            if said:
                self.events.on_transcript(said)
        if sc.get("interrupted") and self.responding:
            self._end(True)
        if sc.get("turnComplete") and self.responding:
            self._end(self._dropping)

    def _end(self, interrupted: bool) -> None:
        self.responding = False
        self._dropping = False
        self.events.on_response_end(interrupted)

    async def add_context(self, text: str) -> None:
        await self.send({"clientContent": {"turns": [{"role": "user", "parts": [{"text": text}]}], "turnComplete": False}})

    async def respond(self, text: str) -> None:
        await self.send({"clientContent": {"turns": [{"role": "user", "parts": [{"text": text}]}], "turnComplete": True}})

    async def cancel(self) -> None:
        if self.responding:
            self._dropping = True


def _explain(reason: str) -> str:
    low = reason.lower()
    if "api key" in low:
        return "Gemini refused the API key: check GEMINI_API_KEY or the key saved in Settings"
    if "not found" in low or "not supported" in low:
        return f"Gemini doesn't offer that model for live voice: {reason}"
    return f"Gemini refused the session: {reason}"
