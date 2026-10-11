"""A live phone call: Twilio's Media Stream (the call's audio, both ways) joined to a realtime speech model that
speaks as the line agent, with only the line agent's tools (``lineagent.py``).

Twilio sends 8 kHz mu-law audio over a WebSocket. Two services can answer:

    GeminiCall   Gemini Live: 16 kHz PCM up, 24 kHz PCM down (converted here, ``codec.py``)
    OpenAICall   OpenAI Realtime: takes and returns mu-law itself, so the audio passes straight through

Turn-taking is the service's own voice detection. When the caller talks over the agent, the agent stops and the audio
Twilio still has queued is cleared (the service sends audio faster than it plays, so Twilio holds a backlog).
``end_call`` hangs up once the goodbye has played: Twilio echoes a mark when it gets there. A call longer than
``phone.limits.call_minutes`` is ended.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

import aiohttp
from aiohttp import WSMsgType, web

from . import codec
from .lineagent import Line, Toolbox, instructions, tools
from .sms import _gemini_schema

log = logging.getLogger(__name__)

MU_LAW_BYTES_PER_S = 8000
IDLE_S = 25                # nobody has said anything for this long (a goodbye without end_call): hang up


class CallSession:
    """The Twilio side and everything both services share; subclasses speak to one service."""
    URL = ""

    def __init__(self, line: Line, settings: dict, toolbox: Toolbox, store, twilio_ws: web.WebSocketResponse,
                 stream_sid: str, key: str, model: str, voice: str, max_s: float, url: str | None = None):
        self.line, self.settings, self.toolbox, self.store = line, settings, toolbox, store
        self.tw, self.stream_sid = twilio_ws, stream_sid
        self.key, self.model, self.voice, self.max_s, self.url = key, model, voice, max_s, url or self.URL
        self.ai: aiohttp.ClientWebSocketResponse | None = None
        self.done = asyncio.Event()
        self.heard_them = asyncio.Event()           # the other party has spoken (an errand waits for "hello?")
        self._speaking_since = 0.0                  # when the agent's current reply started playing
        self._playing_until = 0.0                   # when Twilio will have played all the audio sent so far
        self._bye_mark = False
        self._last_sound = time.monotonic()        # when either side last spoke
        self.reason = ""

    # --- implemented per service ---
    async def _connect(self, http: aiohttp.ClientSession) -> None: ...
    async def _caller_audio(self, ulaw: bytes) -> None: ...
    async def _ai_event(self, m: dict) -> None: ...
    async def _say(self, text: str) -> None: """Have the model speak now, following ``text``."""
    async def _cut_off(self, played_ms: int) -> None: """The caller talked over the agent (audio already cleared)."""

    # --- the call ---
    async def run(self) -> str:
        """Talk until the call ends; returns why it ended."""
        async with aiohttp.ClientSession() as http:
            try:
                await self._connect(http)
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError, RuntimeError) as e:
                log.warning("call: can't start the voice: %s", e)
                if not self.tw.closed:
                    await self.tw.close()
                return f"the voice service could not be reached ({e})"
            tasks = [asyncio.create_task(self._from_twilio()), asyncio.create_task(self._from_ai()),
                     asyncio.create_task(self._start()), asyncio.create_task(self._limit())]
            await self.done.wait()
            for t in tasks:
                t.cancel()
            await self.ai.close()
        if not self.tw.closed:
            await self.tw.close()
        return self.reason or "ended"

    def _end(self, reason: str) -> None:
        if not self.done.is_set():
            self.reason = reason
            self.done.set()

    async def _send_ai(self, msg: dict) -> None:
        if self.ai is not None and not self.ai.closed:
            await self.ai.send_str(json.dumps(msg))

    async def _send_tw(self, msg: dict) -> None:
        if not self.tw.closed:
            await self.tw.send_str(json.dumps({"streamSid": self.stream_sid, **msg}))

    async def _start(self) -> None:
        """Reception greets at once; an errand waits a moment for the other side's "hello?" before speaking."""
        if self.line.job == "errand":
            try:
                await asyncio.wait_for(self.heard_them.wait(), 2.5)
                return                                  # their hello starts the agent's first reply by itself
            except asyncio.TimeoutError:
                pass
        await self._say("The call has just been answered. Greet them now, briefly, and say you're an AI assistant."
                        if self.line.job == "reception" else
                        "The call is connected and nobody has spoken yet. Start: say who you are and why you're calling.")

    async def _limit(self) -> None:
        started = time.monotonic()
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            if now - started > self.max_s:
                self.store.say(self.line.conversation, "system", "Ended: the call reached its time limit")
                self._end("time limit")
                return
            if now - max(self._last_sound, self._playing_until) > IDLE_S:
                self.store.say(self.line.conversation, "system", "Ended: nobody spoke for a while")
                self._end("silence")
                return

    async def _from_twilio(self) -> None:
        async for msg in self.tw:
            if msg.type != WSMsgType.TEXT:
                break
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            event = data.get("event")
            if event == "media" and data.get("media", {}).get("track", "inbound") == "inbound":
                await self._caller_audio(base64.b64decode(data["media"]["payload"]))
            elif event == "mark" and data.get("mark", {}).get("name") == "bye":
                self._end("hung up after goodbye")
                return
            elif event == "stop":
                self._end("the other side hung up")
                return
        self._end("the call's audio stream closed")

    async def _from_ai(self) -> None:
        async for msg in self.ai:
            if msg.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
                break
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            try:
                await self._ai_event(data)
            except Exception:
                log.exception("call: handling the voice service")
        self._end("the voice service closed the connection")

    # --- shared by both services ---
    async def _agent_audio(self, ulaw: bytes) -> None:
        """A piece of the agent's voice, to the caller. Twilio queues what arrives faster than it plays."""
        now = time.monotonic()
        self._last_sound = now
        if now >= self._playing_until:
            self._speaking_since = now
        self._playing_until = max(now, self._playing_until) + len(ulaw) / MU_LAW_BYTES_PER_S
        await self._send_tw({"event": "media", "media": {"payload": base64.b64encode(ulaw).decode()}})

    async def _caller_started(self) -> None:
        """The caller started talking: if the agent's voice is still playing, stop it now."""
        self.heard_them.set()
        now = self._last_sound = time.monotonic()
        if now < self._playing_until:
            await self._send_tw({"event": "clear"})
            await self._cut_off(int((now - self._speaking_since) * 1000))
            self._playing_until = now

    def _tool(self, name: str, args) -> str:
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except ValueError:
                args = {}
        return self.toolbox.run(name, args)

    async def _turn_over(self) -> None:
        """The agent finished a reply: after end_call, hang up once Twilio has played it."""
        if self.line.ending and not self._bye_mark:
            self._bye_mark = True
            await self._send_tw({"event": "mark", "mark": {"name": "bye"}})
            asyncio.get_running_loop().call_later(12, self._end, "hung up after goodbye")

    def _said(self, who: str, text: str) -> None:
        if text.strip():
            self.store.say(self.line.conversation, who, " ".join(text.split()))


class OpenAICall(CallSession):
    URL = "wss://api.openai.com/v1/realtime"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._tool_outputs = False                  # a response used tools: ask for the next one when it ends
        self._item: str | None = None               # the agent's audio item (to truncate when cut off)

    async def _connect(self, http):
        self.ai = await http.ws_connect(self.url, params={"model": self.model}, max_msg_size=0, heartbeat=30,
                                        headers={"Authorization": f"Bearer {self.key}"})
        await self._send_ai({"type": "session.update", "session": {
            "type": "realtime",
            "instructions": instructions(self.line, self.settings),
            "output_modalities": ["audio"],
            "audio": {
                "input": {"format": {"type": "audio/pcmu"},
                          "turn_detection": {"type": "server_vad", "silence_duration_ms": 550, "prefix_padding_ms": 300,
                                             "create_response": True, "interrupt_response": True},
                          "transcription": {"model": "gpt-4o-mini-transcribe"}},
                "output": {"format": {"type": "audio/pcmu"}, "voice": self.voice},
            },
            "tools": [{"type": "function", **t} for t in tools(self.line, self.settings)],
            "tool_choice": "auto",
        }})

    async def _caller_audio(self, ulaw):
        await self._send_ai({"type": "input_audio_buffer.append", "audio": base64.b64encode(ulaw).decode()})

    async def _say(self, text):
        await self._send_ai({"type": "conversation.item.create", "item": {
            "type": "message", "role": "system", "content": [{"type": "input_text", "text": text}]}})
        await self._send_ai({"type": "response.create"})

    async def _cut_off(self, played_ms):
        if self._item:
            await self._send_ai({"type": "conversation.item.truncate", "item_id": self._item, "content_index": 0,
                                 "audio_end_ms": max(0, played_ms)})
            self._item = None

    async def _ai_event(self, m):
        kind = m.get("type", "")
        if kind == "response.output_audio.delta":
            self._item = m.get("item_id") or self._item
            await self._agent_audio(base64.b64decode(m.get("delta", "")))
        elif kind == "input_audio_buffer.speech_started":
            await self._caller_started()
        elif kind == "conversation.item.input_audio_transcription.completed":
            self._said("them", m.get("transcript") or "")
        elif kind == "response.output_audio_transcript.done":
            self._said("agent", m.get("transcript") or "")
        elif kind == "response.function_call_arguments.done":
            result = self._tool(m.get("name", ""), m.get("arguments"))
            await self._send_ai({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": m.get("call_id"), "output": result}})
            self._tool_outputs = True
        elif kind == "response.done":
            if self._tool_outputs:
                self._tool_outputs = False
                await self._send_ai({"type": "response.create"})      # say what came of the tools (or the goodbye)
            else:
                await self._turn_over()
        elif kind == "error":
            err = m.get("error") or {}
            if err.get("code") not in ("response_cancel_not_active", "conversation_already_has_active_response"):
                log.warning("call voice error: %s", err)
                if err.get("type") == "insufficient_quota" or err.get("code") in ("invalid_api_key", "credit_balance_exhausted"):
                    self._end(f"OpenAI refused: {err.get('message') or err.get('code')}")


class GeminiCall(CallSession):
    URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._up, self._down = codec.Up2(), codec.Down3()
        self._them, self._agent = [], []            # transcription pieces of the current turns
        self._dropping = False                      # the reply was cut off: ignore the rest of its audio

    async def _connect(self, http):
        self.ai = await http.ws_connect(self.url, params={"key": self.key}, max_msg_size=0, heartbeat=30)
        fns = [{**t, "parameters": _gemini_schema(t["parameters"])} for t in tools(self.line, self.settings)]
        await self._send_ai({"setup": {
            "model": self.model if self.model.startswith("models/") else f"models/{self.model}",
            "generationConfig": {"responseModalities": ["AUDIO"], "thinkingConfig": {"thinkingBudget": 0},
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice}}}},
            "systemInstruction": {"parts": [{"text": instructions(self.line, self.settings)}]},
            "tools": [{"functionDeclarations": fns}],
            "inputAudioTranscription": {}, "outputAudioTranscription": {},
        }})
        msg = await asyncio.wait_for(self.ai.receive(), 15)
        data = json.loads(msg.data) if msg.type in (WSMsgType.TEXT, WSMsgType.BINARY) else {}
        if "setupComplete" not in data:
            raise RuntimeError(f"Gemini refused the call session: {msg.extra or data or msg.type}")

    async def _caller_audio(self, ulaw):
        pcm16k = self._up(codec.ulaw_to_pcm(ulaw))
        await self._send_ai({"realtimeInput": {"audio": {"data": base64.b64encode(pcm16k.tobytes()).decode(),
                                                         "mimeType": "audio/pcm;rate=16000"}}})

    async def _say(self, text):
        await self._send_ai({"clientContent": {"turns": [{"role": "user", "parts": [{"text": text}]}],
                                               "turnComplete": True}})

    async def _cut_off(self, played_ms):
        self._dropping = True                       # Gemini has stopped; anything still arriving is the old reply

    def _flush_them(self) -> None:
        if self._them:
            self._said("them", "".join(self._them))
            self._them = []

    async def _ai_event(self, m):
        if "toolCall" in m:
            self._flush_them()
            responses = [{"id": c.get("id"), "name": c.get("name"),
                          "response": {"result": self._tool(c.get("name", ""), c.get("args") or {})}}
                         for c in m["toolCall"].get("functionCalls") or []]
            await self._send_ai({"toolResponse": {"functionResponses": responses}})
            return
        sc = m.get("serverContent")
        if not sc:
            return
        heard = (sc.get("inputTranscription") or {}).get("text")
        if heard:
            self._them.append(heard)
            if not self.heard_them.is_set() or time.monotonic() < self._playing_until:
                await self._caller_started()
        if sc.get("interrupted"):
            await self._caller_started()
        parts = (sc.get("modelTurn") or {}).get("parts") or []
        if parts:
            self._flush_them()
        for part in parts:
            blob = part.get("inlineData")
            if blob and str(blob.get("mimeType", "")).startswith("audio/pcm") and not self._dropping:
                pcm24k = codec.np.frombuffer(base64.b64decode(blob["data"]), dtype="<i2")
                await self._agent_audio(codec.pcm_to_ulaw(self._down(pcm24k)))
        said = (sc.get("outputTranscription") or {}).get("text")
        if said:
            self._agent.append(said)
        if sc.get("turnComplete") or sc.get("interrupted"):
            self._said("agent", "".join(self._agent))
            self._agent = []
            self._dropping = False
            if sc.get("turnComplete"):
                await self._turn_over()


def session_class(provider: str) -> type[CallSession]:
    return {"gemini": GeminiCall, "openai": OpenAICall}[provider]
