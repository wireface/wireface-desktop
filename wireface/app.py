"""Wireface: the local backend.

Ties together the face page (``web/``, shown by the Electron shell in ``shell/``), the voice, the microphone and
the mind (a ``backends`` agent session: Claude Code, OpenCode or Codex). The page talks to this process over
``/api`` and one WebSocket (``server``). Everything specific to one face (its config, state, files and port) comes
from its ``Profile``.

Two ways to talk (``voice.mode``):
  realtime  a speech-to-speech model (``realtime/``) listens and answers at once; what you say goes word for word to
            the agent, and the model relays the agent's work and replies from its activity log
  classic   speech recognition (``hearing``) and TTS (``speech.Voice``) on this PC; the agent's replies are read out
The classic voice is also the fallback while the realtime service can't be reached, and rooms always use it.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import os
import re
import secrets
import subprocess
import time

import psutil

from . import config, faceconfig, facemaker, models, server
from . import judge as judge_mod
from .activity import ActivityLog
from .backends import registry
from .backends.base import COMPACTING
from .credentials import Credentials
from .facemaker import FaceMakerError
from . import hearing as hearing_mod
from .hearing import Hearing
from .lipsync import wall_clock
from .profile import Profile, validate
from .realtime import PROVIDERS as REALTIME_PROVIDERS
from .realtime.voice import RealtimeVoice
from .room import names as room_names
from .transcript import Transcript
from .harness_tools import HarnessTools
from .room.face import RoomLink
from .phone.line import PhoneLine
from .tts_elevenlabs import MODELS as ELEVENLABS_MODELS, ElevenLabsClient, ElevenLabsEngine, ElevenLabsError

log = logging.getLogger(__name__)

YES = re.compile(r"^\W*(yes|yeah|yep|sure|ok|okay|allow|go ahead|do it|please do)\b", re.I)
NO = re.compile(r"^\W*(no|nope|don't|do not|deny|stop|cancel)\b", re.I)
# said on its own to the realtime voice: stop talking and stop the work (not a prompt for the agent)
STOP = re.compile(r"^\W*(stop( it| that| talking| working)?|cancel( that)?|never ?mind|be quiet|quiet|shut up|hold on)\W*$", re.I)
# said on its own (the name before or after is fine): the face vanishes from the screen, or comes back. Matched on
# names.norm() text; the agent's work and voice carry on while the face is away.
_POLITE = r"(?: (?:now|please|thanks|thank you|for now|for a (?:bit|while|minute|second)))*"
HIDE = re.compile(r"^(?:please )?(?:go away|go hide|hide|hide yourself|disappear|vanish|get lost|begone|be gone|"
                  r"get out of (?:my|the) way|make yourself scarce)" + _POLITE + "$")
SHOW = re.compile(r"^(?:please )?(?:come back(?: here)?|come here|show yourself|show (?:me )?your face|show up|reappear|"
                  r"appear|return|unhide|where are you|where did you go|where'd you go|i need you|i can't see you|"
                  r"let me see you)" + _POLITE + "$")
# Recognition mangles these short commands ("come back" came out as "Come", "Welcome back.", "You're not back. Show
# your face."), so in the state where one of them is the obvious thing to say, a short utterance made only of these
# words, with a trigger among them, counts too. Anything with other words in it ("go back to the last file") is still
# for the agent.
SHOW_TRIGGERS = {"back", "come", "return", "reappear", "appear", "show", "unhide", "visible", "see"}
SHOW_WORDS = SHOW_TRIGGERS | {
    "please", "now", "you", "you're", "your", "yourself", "face", "are", "not", "aren't", "welcome", "here", "again",
    "on", "me", "let", "i", "can't", "where", "up", "out", "screen", "to", "my", "already", "yet", "still", "why",
    "hey", "ok", "okay", "thanks", "thank", "right", "be", "it", "is", "oh", "so", "just", "the", "can", "could",
    "would", "will"}
HIDE_TRIGGERS = {"away", "hide", "disappear", "vanish", "lost", "begone", "gone"}
HIDE_WORDS = HIDE_TRIGGERS | {
    "go", "please", "now", "for", "a", "bit", "while", "minute", "second", "get", "out", "of", "the", "my", "way",
    "yourself", "your", "face", "thanks", "thank", "you", "just", "ok", "okay", "hey", "can", "could", "would", "be",
    "will", "off", "screen", "from"}
LOOSE_MAX_WORDS = 8


def _loosely(said: str, triggers: set, vocabulary: set) -> bool:
    words = said.split()
    return 0 < len(words) <= LOOSE_MAX_WORDS and any(w in triggers for w in words) and all(w in vocabulary for w in words)


def realtime_trouble(label: str, error: str | None, state: str) -> str:
    """What is wrong with the realtime voice, in plain words (the raw error goes to the activity log)."""
    e = (error or "").lower()
    if "no api key" in e:
        return f"{label} needs an API key. Add one in Settings > Voice."
    if "api key" in e or "401" in e or "403" in e or "unauthenticated" in e or "permission denied" in e:
        return f"{label} didn't accept the API key. Check it in Settings > Voice."
    if "quota" in e or "429" in e or "resource_exhausted" in e or "rate limit" in e:
        return f"{label} has reached its usage limit for now."
    if "model" in e and ("not found" in e or "not supported" in e or "doesn't offer" in e or "404" in e):
        return f"{label} doesn't offer the chosen model. Pick another one in Settings > Voice."
    if "can't reach" in e or "did not answer" in e or "getaddrinfo" in e or "timed out" in e or "connect call failed" in e:
        return f"Can't reach {label}: check the internet connection."
    if "internal error" in e or "1011" in e or "unavailable" in e or "503" in e or "overloaded" in e:
        return f"{label} is having problems on its side."
    return f"{label} can't be used right now." if state == "failed" else f"{label} can't connect right now."


def realtime_session(cfg: dict) -> tuple:
    """The settings a realtime voice session is opened with: changing any of them means a new session."""
    r = cfg["voice"]["realtime"]
    return r["provider"], r[r["provider"]], r["persona"], cfg["backend"]["type"], cfg["backend"]["cwd"], cfg["name"]


class App:
    CLOCK_HEARTBEAT_S = 0.25
    CLOCK_MAX_DRIFT_S = 0.003

    def __init__(self, profile: Profile, shell: bool = True, room: str | None = None):
        self.profile = profile
        self.use_shell = shell
        self.token = secrets.token_urlsafe(24)
        self.port = 0                                # known once the server is listening
        self.face = faceconfig.load(profile.face_file, profile.photo_dir)
        self.clients: set[asyncio.Queue] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.voice = None
        self.voice_error: str | None = None
        self.starting = True
        self.credentials = Credentials(profile)
        self.activity = ActivityLog(profile.logs)
        self.activity.secrets = self.credentials.secret_values()       # keys never reach the stored log
        self.activity.load_history()
        self._cloud_notice: tuple[str, float] = ("", 0.0)
        self.activity.listeners.append(lambda ev: self.broadcast({"type": "activity", "event": ev.to_json()}))
        self.activity.listeners.append(self._to_realtime)
        self.backend = registry.get(self.cfg["backend"]["type"])(self)
        self.realtime: RealtimeVoice | None = None      # the realtime voice, while voice.mode is "realtime"
        # the agent's full session, live (Claude Code's own transcript file), for the panel's transcript view
        self.transcript = Transcript(
            self._transcript_source, lambda entries: self.broadcast({"type": "transcript", "entries": entries}),
            lambda view: self.broadcast({"type": "transcript_reset", **{k: v for k, v in view.items() if k != "entries"}}),
            self.credentials.secret_values)
        self._realtime_outage = False                  # told the user the classic voice is standing in
        self._backend_task: asyncio.Task | None = None       # runs self.backend (see start_backend)
        self.room = RoomLink(self, room or self.cfg["room"]["name"], os.environ.get("WIREFACE_ROOM_DEBUG") == "1")
        self.hearing = Hearing(self._on_voice, self._on_heard, self._on_hearing_error, self._deaf, self._on_name,
                               self._on_meter)
        self._configure_hearing()
        self.speech_status = self._read_speech_status()
        self._installing: asyncio.Task | None = None
        self.permission: dict | None = None          # the request waiting for an answer
        self._permission_answer: asyncio.Future | None = None
        self._permission_lock = asyncio.Lock()
        self._halts = 0                              # how many times the user has said stop
        self.shell_proc: subprocess.Popen | None = None
        self._last_status: dict | None = None
        self._was_speaking = False
        self._clock_sent: tuple | None = None
        self._clock_sent_at = 0.0
        self._clock_seen = None
        self._clock_player = None
        self._named_at = 0.0                         # when the user last stopped the face by saying its name
        self._spoke_at: float | None = None          # when the face was last heard speaking
        # "judge what it hears" (Settings > Voice > Advanced): whether what the microphone heard was said to the face
        self.judge = judge_mod.Judge(lambda: self.credentials.key("openrouter")[0],
                                     lambda: self.cfg["voice"]["hearing"]["judge"]["model"])
        self._judge_lock = asyncio.Lock()            # one utterance at a time, in the order they were said
        self._judge_trouble: tuple[str, float] = ("", 0.0)
        self._held: dict[str, tuple[float, str]] = {}     # activity event id -> (when, words) the judge kept back
        self._fragment: dict | None = None           # a cut-off sentence waiting for the rest of it
        self.face_visible = True                     # False after "go away": the face window is hidden
        self._drawn: dict[str, tuple] = {}           # faces the agent asked for, waiting for the page to map them
        self.phone = PhoneLine(self)                 # the Twilio number (phone/line.py), when it's set up
        self.tools = HarnessTools(self)              # the agent's MCP servers and skills: the panel's Tools tab
        self.activity.listeners.append(self.tools.on_activity)

    # --- this face's settings ----------------------------------------------------------------------------
    @property
    def cfg(self) -> dict:
        return self.profile.config

    @property
    def backend_cfg(self) -> dict:                   # what the backend reads (see backends.base)
        return self.profile.config["backend"]

    @property
    def state(self) -> dict:
        return self.profile.state

    def save_state(self):
        self.profile.save_state()

    # --- to the page -------------------------------------------------------------------------------------
    def broadcast(self, msg: dict):
        for q in self.clients:
            q.put_nowait(msg)

    @staticmethod
    async def _in_thread(fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    def _threadsafe(self, fn, *args):
        self.loop.call_soon_threadsafe(fn, *args)

    def event(self, topic: str, payload: dict | None = None):
        self.broadcast({"type": "event", "ev": {"topic": topic, "payload": payload or {}}})

    def notify(self, text: str):
        log.info("notice: %s", text)
        self.broadcast({"type": "notice", "text": text})

    def phone_event(self, text: str):
        """News from the phone line: in the activity panel, and said out loud now (by the realtime voice, which gets it
        through the activity log, or by the classic voice)."""
        log.info("phone: %s", text)
        self.activity.emit("phone", name="phone", summary=text, status="ok")
        if not self.realtime_speaks:
            self.speak(text)

    def beacon(self) -> dict:
        """The little light in the face's corner: is the agent there, and is it busy."""
        b = self.backend
        if self.permission:
            state, label = "needs_input", "Waiting for your answer"
        elif b.connecting:
            state, label = "connecting", f"Starting {b.label}"
        elif not b.connected and b.last_error:
            state, label = "error", b.last_error
        elif not b.connected:
            state, label = "offline", f"{b.label} is not running" + ("" if self.backend_cfg["auto_start"]
                                                                       else " (starts with your first message)")
        elif b.active or self.room.waiting:
            state, label = "working", "Waiting for its turn" if self.room.waiting and not b.active else b.op or "Working"
        elif b.tasks:
            n = len(b.tasks)
            state, label = "background", f"{n} background task{'s' if n != 1 else ''} running"
        else:
            state, label = "online", f"{b.label} is online"
        return {"state": state, "label": label, "tasks": len(b.tasks), "skip_permissions": bool(
            self.backend_cfg["skip_permissions"])}

    def status(self) -> dict:
        speaking = self._speaking()
        thinking = self.backend.busy
        state = "starting" if self.starting else "speaking" if speaking else "thinking" if thinking else "ready"
        cfg, b = self.cfg, self.backend
        room = self.room.view()
        listening = room["listening"] if room and room["connected"] else cfg["voice"]["listening"]
        return {
            "state": state, "busy_op": b.op if thinking and not speaking else None,
            "agent": cfg["name"], "room": room,
            "mode": {"listening": listening, "voice": cfg["voice"]["on"]},
            "detector": cfg["voice"]["hearing"]["show_detector"],
            "flags": {"speaking": speaking, "thinking": thinking},
            "voice": cfg["voice"]["local"]["voice"], "voices": self.voices, "voice_error": self.voice_error,
            "cwd": self.backend_cfg["cwd"], "permission_mode": self.backend_cfg["permission_mode"],
            "permission_modes": b.permission_modes, "permission": self.permission,
            "skip_permissions": self.backend_cfg["skip_permissions"],
            "backend": {"type": b.id, "label": b.label, "capabilities": sorted(b.capabilities), "applying": b.applying},
            "beacon": self.beacon(), "speech": self._speech(),
            "voice_engine": "realtime" if self.realtime_speaks else "captions" if not self._classic_stands_in()
            else "elevenlabs" if self._use_cloud() else "local",
            "voice_mode": cfg["voice"]["mode"], "realtime": self.realtime.view() if self.realtime else None,
            "session_id": self.state.get("session_id"), "session_cost_usd": b.session_cost,
            "context": b.context, "compact_at": self.backend_cfg.get("compact_at"),
            "compacting": b.op == COMPACTING,
            "face_visible": self.face_visible,
            "phone": {"state": self.phone.state, "error": self.phone.error, "live_calls": len(self.phone.calls)},
        }

    def changed(self):
        """Something the status depends on moved: tell the page (if the status really differs)."""
        s = self.status()
        if s != self._last_status:
            self._last_status = s
            self.broadcast({"type": "status", **s})

    # --- voice -------------------------------------------------------------------------------------------
    @staticmethod
    def _read_speech_status() -> dict:
        rows = models.status()
        return {"models_ok": not models.missing_for("listening") and not models.missing_for("voice"),
                "missing": [r["label"] for r in rows if not r["present"] and not r["optional"]],
                "install_mb": sum(r["size_mb"] for r in rows if not r["present"] and not r["optional"])}

    def _speech(self) -> dict:
        return {**self.speech_status, "installing": bool(self._installing and not self._installing.done())}

    def start_speech_install(self):
        """Download the missing speech models in the background (progress goes to the page), then start the voice
        and, if it was asked for, the microphone: no restart needed."""
        if self._installing and not self._installing.done():
            raise ValueError("the speech models are already being installed")
        self._installing = asyncio.create_task(self._install_speech())
        self.changed()

    async def _install_speech(self):
        loop = asyncio.get_running_loop()
        last = [0.0]

        def progress(key, done, total, phase):                    # from the download thread
            now = time.monotonic()
            if phase == "download" and now - last[0] < 0.25 and not (total and done >= total):
                return
            last[0] = now
            loop.call_soon_threadsafe(self.broadcast, {"type": "models", "key": key, "done": done, "total": total,
                                                       "phase": phase})

        try:
            await self._in_thread(lambda: models.install(progress=progress))
            self.speech_status = self._read_speech_status()
            await self._ensure_voice()
            self._route_mic()
            self.broadcast({"type": "models", "phase": "done"})
        except Exception as e:
            log.exception("installing the speech models")
            self.broadcast({"type": "models", "phase": "error", "error": f"Could not install the speech models: {e}"})
        finally:
            self.changed()

    # --- cloud voice (ElevenLabs) ------------------------------------------------------------------------
    def _eleven_settings(self) -> tuple[str | None, str | None, str]:
        e = self.cfg["voice"]["elevenlabs"]
        return self.credentials.elevenlabs()[0], e["voice_id"], e["model_id"]

    def _use_cloud(self) -> bool:
        """ElevenLabs speaks when it is wanted ("auto" or "elevenlabs") and there is a key for it."""
        if self.cfg["voice"]["engine"] == "local":
            return False
        return bool(self.credentials.elevenlabs()[0])

    def _elevenlabs_view(self) -> dict:
        key, source = self.credentials.elevenlabs()
        return {"available": bool(key), "source": source, "using": self._use_cloud(), "models": ELEVENLABS_MODELS}

    def _on_cloud_error(self, e: ElevenLabsError):
        """From the voice thread: tell the user once (not for every sentence that fails the same way)."""
        def tell():
            text, at = self._cloud_notice
            now = time.monotonic()
            if str(e) != text or now - at > 60:
                self._cloud_notice = (str(e), now)
                self.notify(f"{e}. Using the voice on this PC instead." if self.voice and self.voice.tts else str(e))
        self._threadsafe(tell)

    async def elevenlabs_voices(self) -> list[dict]:
        key = self.credentials.elevenlabs()[0]
        if not key:
            raise ValueError("there is no ElevenLabs API key yet")
        try:
            return await self._in_thread(ElevenLabsClient(key).voices)
        except ElevenLabsError as e:
            raise ValueError(str(e)) from None

    async def set_elevenlabs_key(self, key: str | None):
        """Save (or with None, forget) this face's ElevenLabs key; the page never gets it back."""
        self.credentials.set_elevenlabs(key)
        self.activity.secrets = self.credentials.secret_values()
        await self._ensure_voice()
        self.changed()

    async def test_voice(self) -> str:
        """Say a sentence in whichever voice is in use; returns which one that is."""
        if not self.voice:
            raise ValueError(self.voice_error or "the voice is not ready yet")
        self._stop_voice()
        self.voice.say("This is how I sound.")
        return "elevenlabs" if self._use_cloud() else "local"

    async def _ensure_voice(self):
        """Start the voice if it could not start before (no local models, no key yet), or add the local models to a
        voice that began with only the cloud engine."""
        if self.voice is None:
            try:
                await self._in_thread(self._load_voice)
                self.voice_error = None
            except models.MissingModels as e:
                self.voice_error = str(e)
            except Exception as e:
                log.exception("no voice")
                self.voice_error = f"No voice: {e}"
        elif self.voice.tts is None and not models.missing_for("voice", self.cfg["voice"]["local"]["engine"]):
            if await self._in_thread(self.voice.attach_local):
                self.voices = await self._in_thread(self._list_voices)

    def _list_voices(self) -> list[str]:
        from . import speech

        return speech.pocket_voices() if self.cfg["voice"]["local"]["engine"] == "pocket" else []

    def _load_voice(self):
        from . import speech                      # loads sherpa-onnx: keep it off the import path of the tests

        local = self.cfg["voice"]["local"]
        gone = models.missing_message("voice", local["engine"])
        if gone and not self._use_cloud():
            raise models.MissingModels(gone)
        self.voices = [] if gone else self._list_voices()
        if self.voices and local["voice"] not in self.voices:
            self.cfg["voice"]["local"]["voice"] = self.voices[0]
        self.voice = speech.Voice(
            local["voice"], engine=local["engine"], local=not gone,
            cloud=ElevenLabsEngine(self._eleven_settings), prefer_cloud=self._use_cloud,
            on_cloud_error=self._on_cloud_error,
            on_mouth=lambda f: self._threadsafe(self.broadcast, {"type": "mouth", **f.to_json()}),
            on_sentence=lambda text: self._threadsafe(self._sentence, text))

    voices: list[str] = []

    def _sentence(self, text: str):
        self.event("actor.speaking", {"state": "start", "text": text})
        self.room.speaking("start", text)

    def speak(self, text: str):
        """A piece of the agent's reply. In a room it is spoken only when the room has given this face the floor. With
        the realtime voice it is not read out: the realtime model passes it on from the activity log."""
        if not text or self.realtime_speaks:
            return
        if self.room.joined and self.room.speak(text):
            return
        self._speak_now(text)

    def _classic_stands_in(self) -> bool:
        """The classic voice may speak: always, unless the realtime voice is wanted but down and falling back is off."""
        return not (self.realtime and not self.realtime.handles_speech and not self.cfg["voice"]["realtime"]["fallback"])

    def _speak_now(self, text: str):
        """Say (or, with the voice off, show) a piece of the reply."""
        if self.cfg["voice"]["on"] and self.voice and self._classic_stands_in():
            self.voice.say(text)
        else:
            from .speech import clean_for_speech
            text = clean_for_speech(text)
            if text:
                self.broadcast({"type": "say", "text": text})

    def _clock_message(self, clock: tuple, sr: int, now: float) -> dict | None:
        """Whether a new player clock reading needs sending (the page runs the last one forward itself)."""
        idx, at, end = clock
        if self._clock_sent is not None:
            s0, t0, end0 = self._clock_sent
            predicted = s0 + (at - t0) * sr
            if (end == end0 and abs(predicted - idx) <= self.CLOCK_MAX_DRIFT_S * sr
                    and now - self._clock_sent_at < self.CLOCK_HEARTBEAT_S):
                return None
        self._clock_sent, self._clock_sent_at = (idx, at, end), now
        return {"type": "mouth_clock", "s": int(idx), "t": round(at, 4), "sr": sr,
                "end": None if end is None else int(end)}

    async def _tick(self):
        """The speaker clock for lip sync, the end of speech, and status changes."""
        while True:
            await asyncio.sleep(0.02)
            player = self._player()
            if player is not self._clock_player:          # another voice took over: its sample count is its own
                self._clock_player, self._clock_sent, self._clock_seen = player, None, None
            if player:
                clock = player.clock
                if clock is not None and clock is not self._clock_seen:
                    self._clock_seen = clock
                    msg = self._clock_message(clock, player.sr, wall_clock())
                    if msg:
                        self.broadcast(msg)
            speaking = self._speaking()
            if speaking:
                self._spoke_at = time.monotonic()
            if self._was_speaking and not speaking:
                self.event("actor.speaking", {"state": "stop"})
                self.room.speaking("stop")
            self._was_speaking = speaking
            self.room.poll()
            self.changed()

    # --- from the page (and the microphone) -----------------------------------------------------------------
    def chat(self, text: str):
        text = text.strip()
        if not text:
            return
        log.info("user: %s", text)
        if self.room.joined and self.room.active:      # in a room, what is typed here is put to this face's agent first
            self.room.say(text)
            return
        if self.realtime_speaks:
            self.realtime.typed(text)                  # it should know, and it stops talking
        if self._speaking():                           # a new message cuts off what was being said
            self._stop_voice()
        self.backend.send(text)
        self.changed()

    def _stop_voice(self):
        if self.voice:
            self.voice.stop()
        if self.realtime:
            self.realtime.cut_off()
        self._clock_sent = None
        self.broadcast({"type": "mouth_flush"})

    def _speaking(self) -> bool:
        return bool(self.voice and self.voice.busy) or bool(self.realtime and self.realtime.speaking)

    def _player(self):
        """The audio player the face's mouth follows: the realtime voice's while it speaks for the face."""
        if self.realtime_speaks and self.realtime.speaker:
            return self.realtime.speaker.player
        return self.voice.player if self.voice else None

    async def halt(self, from_room: bool = False):
        """Stop talking and stop working (in a room, for everyone: the room tells the others)."""
        if self.room.joined and not from_room:
            self.room.halt()
        self._stop_voice()
        if self.realtime:
            self.realtime.halt()
        self._halts += 1
        self.answer_permission(False)
        await self.backend.halt()
        self.event("control.halt")
        self.changed()

    # --- settings ------------------------------------------------------------------------------------------
    async def set_config(self, patch: dict) -> dict:
        """Change this face's config (``profile.validate`` says what is allowed), apply it, save it."""
        before = copy.deepcopy(self.cfg)
        new = validate(patch, before)                    # ValueError: nothing changed
        b0, b1 = before["backend"], new["backend"]
        v0, v1 = before["voice"], new["voice"]
        self.profile.config = new
        if b1["cwd"] != b0["cwd"]:                       # a conversation belongs to its folder
            self.state["session_id"], self.state["sessions"] = None, {}
            self.save_state()
        if v1["listening"] != v0["listening"] and self.room.joined:
            self.room.set_listening(v1["listening"])           # the room owns the microphone
        if new["room"] != before["room"]:
            await self.room.rejoin(new["room"]["name"])
        # (the judge is not the ears, and the voice detector's meter only watches them)
        ears = lambda v: {k: x for k, x in v["hearing"].items() if k not in ("judge", "show_detector")}
        if (ears(v1), new["name"], new["room"]) != (ears(v0), before["name"], before["room"]):
            self._configure_hearing()
        elif v1["hearing"]["show_detector"] != v0["hearing"]["show_detector"]:
            self.hearing.configure(meter=v1["hearing"]["show_detector"])
        if not v1["on"] and v0["on"]:
            self._stop_voice()
        if v1["local"]["voice"] != v0["local"]["voice"] and self.voice:
            await self._in_thread(self.voice.set_voice, v1["local"]["voice"])
        if v1["engine"] != v0["engine"]:
            await self._ensure_voice()
        if b1["type"] != b0["type"]:
            await self.switch_backend(b1["type"])
        elif b1 != b0:
            await self.backend.apply_config()
        if new["phone"] != before["phone"]:
            await self.phone.restart()
            if new["phone"]["enabled"] != before["phone"]["enabled"] and b1 == b0 and b1["type"] == b0["type"]:
                await self.backend.apply_config()             # the agent gains (or loses) its phone tools
        if self.realtime and self._realtime_wanted() and realtime_session(before) != realtime_session(new):
            await self.realtime.restart()                     # it was told who it is and what it fronts
        elif self.realtime and v1["realtime"]["narration"] != v0["realtime"]["narration"]:
            self.realtime.set_level(v1["realtime"]["narration"])
        await self._apply_realtime()
        await self._in_thread(self.profile.save_config)
        self.changed()
        return new

    def start_backend(self):
        self._backend_task = asyncio.create_task(self.backend.run(), name="backend")

    async def switch_backend(self, kind: str):
        """Run a different agent behind the face. Each agent keeps its own conversation (they cannot read each
        other's), so switching back picks the earlier one up again."""
        old, task = self.backend, self._backend_task
        await old.halt()
        old.shutdown()
        if task:
            try:
                await asyncio.wait_for(task, 15)
            except Exception:
                log.exception("stopping %s", old.id)
        self.permission = None
        sessions = self.state.setdefault("sessions", {})
        sessions[old.id] = self.state.get("session_id")
        self.state["session_id"] = sessions.get(kind)
        self.save_state()
        self.backend = registry.get(kind)(self)
        self.start_backend()
        self.activity.emit("notice", summary=f"Now running {self.backend.label}")
        self.changed()

    # --- the realtime voice ----------------------------------------------------------------------------------
    def _realtime_wanted(self) -> bool:
        v = self.cfg["voice"]
        return v["mode"] == "realtime" and v["on"] and not self.room.joined

    @property
    def realtime_speaks(self) -> bool:
        """The realtime voice talks for the face now (otherwise the classic voice does)."""
        return bool(self.realtime and self.realtime.handles_speech)

    async def _apply_realtime(self):
        """Start or stop the realtime voice to match the settings, and give the microphone to whichever voice listens."""
        if self._realtime_wanted() and not self.realtime:
            self.realtime = RealtimeVoice(self)
            await self.realtime.start()
        elif not self._realtime_wanted() and self.realtime:
            rt, self.realtime = self.realtime, None
            await rt.stop()
            self._realtime_outage = False
        self._route_mic()

    def _route_mic(self):
        """The microphone belongs to the room (in a room), otherwise to the speech recognition on this PC, whichever
        voice is speaking: the realtime model never hears the user."""
        if self.cfg["voice"]["listening"] and not self.room.joined:
            self.hearing.start()
        else:
            self.hearing.stop()

    def _deaf(self) -> bool:
        """The microphone ignores the face's own voice (unless the realtime voice may be interrupted by talking)."""
        if self.realtime_speaks:
            return self.realtime.deaf or bool(self.voice and self.voice.busy)
        return self._speaking()

    def realtime_changed(self):
        """The realtime voice connected, dropped or gave up: the classic voice stands in while it is away."""
        rt = self.realtime
        if not rt:
            return
        if rt.handles_speech:
            if self._realtime_outage and rt.state == "connected":
                self._realtime_outage = False
                self.notify(f"{rt.label} is back.")
        elif not self._realtime_outage:
            self._realtime_outage = True
            trouble = realtime_trouble(rt.label, rt.error, rt.state)
            retrying = rt.state != "failed"
            if self.cfg["voice"]["on"] and self.voice and self._classic_stands_in():
                then = "The voice on this PC reads the replies" + (" until it's back." if retrying else " instead.")
            else:
                then = "Replies show as text" + (" until it's back." if retrying else " for now.")
            self.notify(f"{trouble} {'It keeps trying to reconnect. ' if retrying else ''}{then}")
            why = rt.error or rt.state
            self.activity.emit("notice", summary=trouble, output=why if why.startswith(rt.label) else f"{rt.label}: {why}")
        self._route_mic()
        self.changed()

    def _transcript_source(self) -> tuple[str, str] | None:
        """The session whose transcript the panel follows: Claude Code's current one (other agents keep theirs in their
        own formats; the activity log shows their work)."""
        sid = self.state.get("session_id")
        return (self.backend_cfg["cwd"], sid) if self.backend.id == "claude" and sid else None

    def _to_realtime(self, ev):
        if self.realtime:
            self.realtime.activity(ev.to_json())

    def microphone_failed(self, message: str):
        self.cfg["voice"]["listening"] = False
        self._route_mic()
        self.notify(message)
        self.changed()

    def realtime_model_source(self, provider: str) -> tuple[str, str | None, str] | None:
        """(label, API key, default model) for asking a realtime service which models it has; None if unknown."""
        cls = REALTIME_PROVIDERS.get(provider)
        if cls is None:
            return None
        return cls.label, self.credentials.realtime_key(provider)[0], cls.default_model

    def _realtime_view(self) -> dict:
        providers = {}
        for pid, cls in REALTIME_PROVIDERS.items():
            key, source = self.credentials.realtime_key(pid)
            providers[pid] = {"label": cls.label, "available": bool(key), "source": source, "voices": list(cls.voices),
                              "default_model": cls.default_model, "key_envs": list(cls.key_envs)}
        live = self.realtime.view() if self.realtime else None
        if live and live["error"]:
            live["problem"] = realtime_trouble(live["label"], live["error"], live["state"])
        return {"providers": providers, "live": live}

    async def set_realtime_key(self, provider: str, key: str | None):
        """Save (or with None, forget) this face's key for a realtime service; the page never gets it back."""
        if provider not in REALTIME_PROVIDERS:
            raise ValueError(f"unknown realtime service {provider!r}")
        self.credentials.set_key(provider, key)
        self.activity.secrets = self.credentials.secret_values()
        if self.realtime and provider == self.cfg["voice"]["realtime"]["provider"]:
            await self.realtime.restart()
            self._route_mic()
        self.changed()

    def config_view(self) -> dict:
        """What the settings page needs: the config and the choices that go with it. Never any secrets."""
        b = self.backend
        return {
            "realtime": self._realtime_view(), "judge": self._judge_view(),
            "config": self.cfg, "file": str(self.profile.file), "face": self.profile.name, "port": self.port,
            "backends": [{"type": t, "label": registry.get(t).label} for t in registry.types()],
            "backend": {"type": b.id, "label": b.label, "capabilities": sorted(b.capabilities),
                        "permission_modes": b.permission_modes, "permission_help": b.permission_help,
                        "applying": b.applying},           # settings waiting for the agent to restart
            "voices": self.voices, "speech": self._speech(),
            "elevenlabs": self._elevenlabs_view(), "room": self.room.view(), "phone": self.phone.view(),
        }

    async def set_phone_token(self, key: str | None):
        """Save (or with None, forget) this face's Twilio auth token; the page never gets it back."""
        self.credentials.set_key("twilio", key)
        self.activity.secrets = self.credentials.secret_values()
        await self.phone.restart()
        self.changed()

    async def phone_agent(self, tool: str, args: dict) -> str:
        """The agent used a phone tool; ValueError says why it couldn't be done."""
        from .phone.line import PhoneError
        from .phone.twilio import TwilioError
        try:
            return await self.phone.agent_tool(tool, args)
        except (PhoneError, TwilioError) as e:
            raise ValueError(str(e)) from None

    async def test_phone(self) -> str:
        """Check the account and number work (nothing is sent)."""
        from .phone.line import PhoneError
        from .phone.twilio import TwilioError
        try:
            tw = self.phone.twilio()
            account = await tw.account()
            record = await tw.number(self.cfg["phone"]["number"])
        except (PhoneError, TwilioError) as e:
            raise ValueError(str(e)) from None
        if record is None:
            raise ValueError(f"{self.cfg['phone']['number']} is not a number on this Twilio account")
        return f"Connected to {account.get('friendly_name') or 'the account'}; {record.get('phone_number')} is ready."

    async def set_mode(self, patch: dict):
        """The menu and toolbar's settings (kept flat); each maps onto the config."""
        nested: dict = {}
        if "listening" in patch:
            nested.setdefault("voice", {})["listening"] = bool(patch["listening"])
        if "voice" in patch:
            nested.setdefault("voice", {})["on"] = bool(patch["voice"])
        if "voice_mode" in patch:
            nested.setdefault("voice", {})["mode"] = patch["voice_mode"]
        if "voice_name" in patch:
            name = str(patch["voice_name"])
            if name not in self.voices:
                raise ValueError(f"unknown voice {name!r}")
            nested.setdefault("voice", {}).setdefault("local", {})["voice"] = name
        for key in ("permission_mode", "cwd", "skip_permissions"):
            if key in patch:
                nested.setdefault("backend", {})[key] = patch[key]
        if nested:
            await self.set_config(nested)

    def turn_started(self, source: str = "user"):
        """A backend began a turn (``source``: "user" for a prompt it was given, "background" for its own)."""
        if self.room.joined:
            self.room.turn_started(source)

    def turn_done(self, source: str = "user"):
        if self.room.joined:
            self.room.turn_done(source)

    def compact_conversation(self):
        """Summarise the conversation so far, so a long one can go on (the activity log says how it went)."""
        if "compact" not in self.backend.capabilities:
            self.notify(f"{self.backend.label} can't compact its conversation.")
            return
        self.backend.compact()

    def new_conversation(self):
        self._stop_voice()
        self.backend.new_conversation()
        if self.realtime:
            self.realtime.new_conversation()
        self.notify("New conversation.")

    # --- permission prompts ---------------------------------------------------------------------------------
    async def ask_permission(self, show: str, say: str, can_always: bool) -> tuple[bool, bool]:
        """Put an agent's permission request to the user; returns (allow, don't ask again this session).

        An agent may ask for several tool calls at once: they are put to the user one at a time.
        """
        halts = self._halts
        async with self._permission_lock:
            if halts != self._halts:                   # stopped while this one was waiting its turn
                return False, False
            answer = self._permission_answer = self.loop.create_future()
            self.permission = {"id": secrets.token_hex(4), "text": show[:400], "can_always": can_always}
            self.room.permission(True)
            self.changed()
            if self.realtime_speaks:
                self.realtime.permission(show, say)    # the realtime voice asks, in its own words
            else:
                self.speak(say)
            try:
                return await answer
            finally:
                self.permission = self._permission_answer = None
                self.room.permission(False)
                self.changed()

    def answer_permission(self, allow: bool, always: bool = False, request_id: str | None = None) -> bool:
        fut = self._permission_answer
        if not fut or fut.done() or (request_id and request_id != self.permission["id"]):
            return False
        fut.set_result((allow, always and allow))
        return True

    # --- microphone (called from the listening thread) ---------------------------------------------------------
    # --- its name: stop talking and listen ------------------------------------------------------------------------
    def names(self) -> list[str]:
        """The ways of saying this face's name (its name, the room's "also called" list, how recognition spells it)."""
        return room_names.aliases_for(self.cfg["name"], "", self.cfg["room"]["aliases"])

    def _configure_hearing(self):
        h = self.cfg["voice"]["hearing"]
        # the spotter listens for the name and the names the user added, not recognition's misspellings of it ("clod"
        # sounds like "cloud"); in a room the room process listens, and it has its own way of hearing names
        spoken = [self.cfg["name"], *self.cfg["room"]["aliases"]]
        self.hearing.configure(names=spoken if h["name_interrupt"] and not self.room.joined else [],
                               denoise=h["denoise"], focus=h["focus"], gate=h["surprise"], meter=h["show_detector"])

    def _on_meter(self, reading: dict):
        """What the microphone measured ("show voice detector"): for the meter on the face and in the settings."""
        self._threadsafe(self.broadcast, {"type": "meter", **reading})

    def _on_name(self, word: str):
        self._threadsafe(self._name_heard, word)

    def _name_heard(self, word: str) -> bool:
        """The microphone heard the face's name. If the face is talking (and it isn't its own voice saying the name),
        it stops and listens at once, so the user can cut in. Returns whether it stopped."""
        if not self._speaking():
            return False
        if self._said_own_name():
            log.info("name heard, but the face itself just said it: not stopping")
            return False
        log.info("name heard (%s): stopping to listen", word)
        self._named_at = time.monotonic()
        if self.realtime:
            self.realtime.halt()                       # stops talking and forgets what was waiting to be said
        self._stop_voice()
        self.hearing.listen_now()
        self.event("percept.audio.partial")
        self.activity.emit("notice", summary="You said its name: stopped talking to listen")
        self.changed()
        return True

    ECHO_WINDOW_S = 2.5                                # the face's own words this recent can reach the microphone

    def _said_own_name(self) -> bool:
        """Whether the words coming out of the speakers right now (or just before) contain the face's name."""
        voice = self.realtime.speaker if self.realtime_speaks and self.realtime and self.realtime.speaker else self.voice
        if voice is None or not getattr(voice, "spoken", None):
            return False
        player = voice.player
        clock = player.clock
        if clock is None:
            return False
        first, at, end = clock
        now = first + (wall_clock() - at) * player.sr
        if end is not None:
            now = min(now, end)
        lo, hi = now - self.ECHO_WINDOW_S * player.sr, now + 0.2 * player.sr
        entries = list(voice.spoken)
        aliases = {"me": self.names()}
        for i, (start, text) in enumerate(entries):
            stop = entries[i + 1][0] if i + 1 < len(entries) else player.written
            if stop >= lo and start <= hi and (room_names.find_names(text, aliases)
                                               or hearing_mod.sounds_like(text, [self.cfg["name"], *self.cfg["room"]["aliases"]])):
                return True
        return False

    STEER_WINDOW_S = 12.0                              # what is said this soon after stopping the face by name

    def _steering(self, text: str) -> bool:
        """The user addressed the face by name (just now, to stop it, or at the start of this sentence) while the agent
        is still working: this replaces what it is doing rather than waiting behind it ("Claude, ignore that...")."""
        if not self.backend.busy:
            return False
        if time.monotonic() - self._named_at < self.STEER_WINDOW_S:
            return True
        d = room_names.parse_directed(text, {"me": self.names()})
        return d.member == "me" and bool(d.text.strip())

    async def _steer(self, text: str):
        await self.backend.halt()                      # stop the current request (and drop any waiting behind it)
        self.activity.emit("notice", summary="Stopped the current request for the new one")
        self.backend.send(text)
        self.changed()

    def _name_only(self, text: str) -> bool:
        """Just the name ("Claude?", "Hey Claude"): the user has the face's attention; nothing to send."""
        d = room_names.parse_directed(text, {"me": self.names()})
        return d.member == "me" and not d.text.strip()

    def _on_voice(self):
        self._threadsafe(self._user_voice)

    def _user_voice(self):
        self.event("percept.audio.partial")
        if self.realtime_speaks:
            self.realtime.user_voice()             # it waits for the user (and, with barge-in, stops talking)
        if self._fragment:                         # the rest of a cut-off sentence is coming: keep waiting for it
            self._fragment["timer"].cancel()
            self._fragment["timer"] = self.loop.call_later(judge_mod.MERGE_WAIT_S, self._fragment_done)

    def _on_heard(self, text: str, info: dict | None = None):
        self._threadsafe(self.heard, text, info)

    def heard(self, text: str, info: dict | None = None):
        """What the user said (speech recognition on this PC; ``info``: how it sounded, see ``hearing.Ears``). With the
        realtime voice the words go to the agent word for word and the voice gets a note of them (it never answers
        them itself); otherwise it is as if they were typed. With "judge what it hears" on, the judge (``judge.py``)
        first decides whether they were said to the face at all, and what they are."""
        self.event("percept.audio.transcript", {"text": text})
        realtime = self.realtime_speaks
        if self._name_only(text):                      # it stopped and is listening: wait for what comes next
            log.info("user said just the name: listening")
            if realtime:
                self.activity.emit("voice", name="user", summary=text[:300], output=text)
            return
        if self.permission:                            # "yes" / "no" answers the question on the table
            answer = True if YES.match(text) else False if NO.match(text) else None
            if answer is not None:
                self.answer_permission(answer)
                if realtime:
                    self.activity.emit("voice", name="user", summary=text[:300], output=text)
                return
        visible = self.face_command(text)
        if visible is not None:                        # "go away" / "come back": for the face, not the agent
            self.activity.emit("voice", name="user", summary=text[:300], output=text)
            self.show_face(visible)
            return
        judging = self._judging()
        if not realtime and not judging:
            self.chat(text)
            return
        log.info("user (spoken): %s", text)
        if STOP.match(text):                           # "stop" on its own: stop, don't ask the agent to
            self.activity.emit("voice", name="user", summary=text[:300], output=text)
            asyncio.ensure_future(self.halt())
            return
        if judging:
            asyncio.ensure_future(self._judged(text, info or {}))
            return
        self._deliver(text)

    def _deliver(self, text: str):
        """Words said to the face go to the agent: in place of what it is doing if the user addressed it by name while
        it works, otherwise behind it."""
        if self._steering(text):
            self._named_at = 0.0
            if self.realtime_speaks:
                self.realtime.heard(text, False)
            asyncio.ensure_future(self._steer(text))
            return
        if not self.realtime_speaks:
            self.chat(text)
            return
        queued = self.backend.busy
        self.backend.send(text)
        self.realtime.heard(text, queued)
        self.changed()

    # --- the judge: was it said to the face? ------------------------------------------------------------------------
    HELD_S = 600.0                                     # how long what the judge kept back can still be sent on

    def _judging(self) -> bool:
        return bool(self.cfg["voice"]["hearing"]["judge"]["on"])

    async def _judged(self, text: str, info: dict):
        async with self._judge_lock:
            fragment, self._fragment = self._fragment, None
            if fragment:                               # the rest of a cut-off sentence: judge them together
                fragment["timer"].cancel()
                text = f"{fragment['text']} {text}"
            try:
                verdict = await self.judge.decide(text, info, self._moment())
            except judge_mod.JudgeUnavailable as e:
                self._judge_unavailable(str(e))
                self._deliver(text)
                return
            how = {"named": room_names.parse_directed(text, {"me": self.names()}).member == "me",
                   "standing": info.get("stands_out"), "unsure": self.cfg["voice"]["hearing"]["judge"]["unsure"],
                   "permission": bool(self.permission)}
            action = judge_mod.route(verdict, can_wait=not fragment, **how)
            if action.do == "wait":
                self._fragment = {"text": text, "info": info, "verdict": verdict, "how": how,
                                  "timer": self.loop.call_later(judge_mod.MERGE_WAIT_S, self._fragment_done)}
                return
            self._act(text, info, verdict, action)

    def _fragment_done(self):
        """Nothing more came after a cut-off sentence: it is taken as it is."""
        fragment, self._fragment = self._fragment, None
        if fragment:
            action = judge_mod.route(fragment["verdict"], can_wait=False, **fragment["how"])
            self._act(fragment["text"], fragment["info"], fragment["verdict"], action)

    def _act(self, text: str, info: dict, verdict: judge_mod.Verdict, action: judge_mod.Action):
        do = action.do
        sound = {k: info.get(k) for k in ("duration_s", "level_vs_user_db", "lift_db", "stands_out")}
        ev = self.activity.emit("heard", name=do, summary=text[:300], output=text,
                                extra={"action": do, "why": action.why, **verdict.summary(), "sound": sound})
        if do in ("drop", "hold"):                     # kept back: the panel can still send it on
            self._forget_held()
            self._held[ev.id] = (time.monotonic(), text)
            log.info("judge: %s %r (%s)", "dropped" if do == "drop" else "held", text[:80], action.why)
            self.event("percept.audio.judged", {"text": text, "status": "ignored" if do == "drop" else "held"})
        elif do == "stop":
            asyncio.ensure_future(self.halt())
        elif do in ("approve", "decline"):
            self.answer_permission(do == "approve")
        elif do in ("hide", "show"):
            self.show_face(do == "show")
        elif do == "compact":
            self.compact_conversation()
        elif do == "new_conversation":
            self.new_conversation()
        else:
            self._deliver(text)

    def _moment(self) -> judge_mod.Moment:
        """What was going on, for the judge: the last few lines of the conversation, what the agent is doing."""
        recent: list[tuple[str, str]] = []
        last = None
        for ev in self.activity.recent(150):
            kind, x = ev["kind"], ev.get("extra") or {}
            if kind == "turn" and ev.get("input") and x.get("source") != "background":
                line = ("user", ev["input"])
            elif kind == "assistant" and not ev.get("parent_id") and ev.get("output"):
                line = ("assistant", ev["output"])
            else:
                continue
            last = ev["ts"]
            if recent and recent[-1][0] == line[0]:    # a reply in several pieces is one line
                recent[-1] = (line[0], f"{recent[-1][1]}\n{line[1]}")
            else:
                recent.append(line)
        b = self.backend
        return judge_mod.Moment(
            name=self.cfg["name"], aliases=list(self.cfg["room"]["aliases"]), recent=recent[-judge_mod.RECENT:],
            working_on=(b.op or "a request") if b.busy else None,
            permission=self.permission["text"] if self.permission else None, face_visible=self.face_visible,
            spoke_s=None if self._spoke_at is None else time.monotonic() - self._spoke_at,
            quiet_s=None if last is None else max(0.0, time.time() - last))

    def _judge_unavailable(self, why: str):
        """The judge couldn't be asked, so the words went to the agent as before: say so now and then, not each time."""
        last, at = self._judge_trouble
        now = time.monotonic()
        if why == last and now - at < 600:
            return
        self._judge_trouble = (why, now)
        log.warning("judge unavailable: %s", why)
        self.activity.emit("notice", summary="Couldn't judge what it heard, so the words went to the agent", output=why)
        if "no OpenRouter API key" in why:
            self.notify("Judging what it hears needs an OpenRouter API key: Settings > Voice > Advanced.")

    def _forget_held(self):
        now = time.monotonic()
        for eid in [eid for eid, (at, _) in self._held.items() if now - at > self.HELD_S]:
            del self._held[eid]

    def send_held(self, eid: str) -> bool:
        """Send on something the judge kept back (the panel's Send button); False if it is gone."""
        self._forget_held()
        item = self._held.pop(eid, None)
        if item is None:
            return False
        self.activity.update(eid, extra={"sent": True})
        log.info("user sent on what the judge kept back: %r", item[1][:80])
        self._deliver(item[1])
        return True

    def _judge_view(self) -> dict:
        key, source = self.credentials.key("openrouter")
        return {"available": bool(key), "source": source, "calls": self.judge.calls,
                "spent_usd": round(self.judge.spent_usd, 6), "error": self.judge.last_error,
                "default_model": judge_mod.DEFAULT_MODEL}

    async def set_judge_key(self, key: str | None):
        """Save (or with None, forget) this face's OpenRouter key; the page never gets it back."""
        self.credentials.set_key("openrouter", key)
        self.activity.secrets = self.credentials.secret_values()
        self.changed()

    def _on_hearing_error(self, message: str):
        self._threadsafe(self.microphone_failed, message)

    def face_command(self, text: str) -> bool | None:
        """False for "go away" (or "Claude, hide"), True for "come back", None for anything else. While the face is
        away, anything short that sounds like asking it back brings it back; while it's here, the same for going."""
        aliases = self.names()
        said = room_names.parse_directed(text, {"me": aliases}).text
        for alias in aliases:                          # "go away, Claude"
            if said.endswith(" " + alias):
                said = said[:-len(alias) - 1]
                break
        if HIDE.match(said):
            return False
        if SHOW.match(said):
            return True
        names = {w for a in aliases for w in a.split()}
        if not self.face_visible and _loosely(said, SHOW_TRIGGERS, SHOW_WORDS | names):
            return True
        if self.face_visible and _loosely(said, HIDE_TRIGGERS, HIDE_WORDS | names):
            return False
        return None

    def show_face(self, visible: bool):
        """Hide the face (it dissolves, then the window goes) or bring it back (it materialises again): the voice
        commands, the agent's face tool, and the page itself (the tray icon and the menu)."""
        self.face_visible = bool(visible)
        log.info("face %s", "back" if self.face_visible else "away")
        self.event("face.visibility", {"visible": self.face_visible})
        self.changed()

    # --- face settings ------------------------------------------------------------------------------------------
    EXPRESSION_MAX_S = 30.0

    def express(self, name: str, seconds: float = 4.0) -> str:
        """Put an expression on the face for a few seconds (the agent's face tool, the panel's preview buttons)."""
        key = faceconfig.expression(name)
        seconds = max(0.0, min(self.EXPRESSION_MAX_S, float(seconds)))
        self.event("face.expression", {"name": key, "seconds": seconds if key != "neutral" else 0})
        return key

    async def _save_face(self, cfg: dict):
        await self._in_thread(faceconfig.save, cfg, self.profile.face_file)
        self.face = cfg
        self.broadcast({"type": "face_config", "config": cfg})

    async def set_face(self, patch: dict) -> dict:
        cfg = faceconfig.validate(patch, self.face)
        await self._save_face(cfg)
        return cfg

    async def _wear(self, photo: dict) -> dict:
        """A new skin on the face. With the slider at the wireframe it would not show: then it comes on in full."""
        cfg = {**self.face, "photo": photo, "style": "photo"}
        if cfg.get("skin_amount", 1) < 0.05:
            cfg["skin_amount"] = 1.0
        await self._save_face(cfg)
        return cfg

    async def save_face_photo(self, jpeg: bytes, landmarks: list, width: int, height: int,
                              portrait: str | None = None) -> dict:
        """A photo mapped in the page (which found its landmarks); ``portrait``: the built-in one it is, if it is."""
        await self._in_thread(faceconfig.write_photo, self.profile.photo_dir, jpeg, landmarks)
        known = portrait if any(p["id"] == portrait for p in faceconfig.portraits()) else None
        return await self._wear({"version": secrets.token_hex(4), "w": int(width), "h": int(height), "portrait": known})

    async def use_portrait(self, pid: str) -> dict:
        """Put on a built-in portrait: its landmarks come with it, so no face finder is needed."""
        return await self._wear(await self._in_thread(faceconfig.wear_portrait, pid, self.profile.photo_dir))

    async def delete_face_photo(self) -> dict:
        """Take the photo off: the face wears the default portrait again."""
        def reset():
            for name in ("photo.jpg", "landmarks.json"):
                (self.profile.photo_dir / name).unlink(missing_ok=True)
            return faceconfig.default_photo(self.profile.photo_dir)

        cfg = {**self.face, "photo": await self._in_thread(reset), "style": "photo"}
        await self._save_face(cfg)
        return cfg

    # --- a face the agent asks for (its set_face tool): drawn by wireface.dev, mapped by the face page -----------------
    MAP_TIMEOUT_S = 60

    async def make_face(self, who: str) -> tuple[dict, bytes]:
        """A portrait of ``who`` (``facemaker``), worn. The face page finds its landmarks, as for a photo dropped on it:
        told the picture is ready, it fetches it (``drawn_face``) and sends back what it found (``drawn_face_mapped``).
        -> (the face's new config, the picture as worn); FaceMakerError if it can't be done."""
        if not self.clients:
            raise FaceMakerError("the face window isn't open to put it on")
        image, mime = await facemaker.draw(who)
        job = secrets.token_hex(8)
        done = asyncio.get_running_loop().create_future()
        self._drawn[job] = (image, mime, done)
        self.broadcast({"type": "face_drawn", "id": job})
        try:
            return await asyncio.wait_for(done, self.MAP_TIMEOUT_S)
        except TimeoutError:
            raise FaceMakerError("the face window didn't put it on (is it open?)") from None
        finally:
            self._drawn.pop(job, None)

    def drawn_face(self, job: str) -> tuple[bytes, str] | None:
        """(picture, MIME type) of a drawn face waiting to be mapped; None if there is none."""
        entry = self._drawn.get(job)
        return entry[:2] if entry else None

    async def drawn_face_mapped(self, job: str, jpeg: bytes | None, landmarks: list, width: int, height: int,
                                error: str | None = None) -> dict:
        """What the face page found in a drawn face: it is worn (or ``error`` says why the page couldn't map it). The
        first page to answer does it; KeyError for a face nobody is waiting for."""
        entry = self._drawn.pop(job, None)
        if entry is None or entry[2].done():
            raise KeyError(job)
        done = entry[2]
        if error:
            done.set_exception(FaceMakerError(f"the face finder couldn't map it ({error}); a description with a clear, "
                                              "front-on face may work"))
            return self.face
        try:
            cfg = await self.save_face_photo(jpeg, landmarks, width, height)
        except (ValueError, TypeError, OSError) as e:
            if not done.done():
                done.set_exception(FaceMakerError(f"couldn't put it on: {e}"))
            raise
        if not done.done():                          # (unless the agent stopped waiting meanwhile)
            done.set_result((cfg, jpeg))
        return cfg

    # --- desktop shell ------------------------------------------------------------------------------------------
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/face#t={self.token}"

    def start_shell(self):
        profile = f"--face-profile={self.profile.shell_profile}"
        # A shell left over from a process that died still holds this profile's single-instance lock (so
        # ours would quit at once) and points at the old process's token.
        for p in psutil.process_iter(["cmdline"]):
            try:
                if profile in (p.info["cmdline"] or []):
                    log.info("closing a shell left over from an earlier run (pid %d)", p.pid)
                    for child in p.children(recursive=True):
                        child.kill()
                    p.kill()
            except psutil.Error:
                pass
        cmd = [str(config.ELECTRON_EXE), str(config.SHELL_RUNTIME), f"--face-url={self.url}",
               f"--face-state={self.profile.window_file}", profile]
        self.profile.logs.mkdir(parents=True, exist_ok=True)
        with open(self.profile.logs / "shell.log", "ab") as log_file:
            env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
            self.shell_proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT, env=env,
                                               cwd=str(config.SHELL_RUNTIME),
                                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    # --- lifecycle ------------------------------------------------------------------------------------------------
    async def main(self):
        self.loop = asyncio.get_running_loop()
        runner = await server.start(self)
        self.start_backend()
        self.room.start()
        tasks = [asyncio.create_task(self._tick())]
        if self.use_shell and config.ELECTRON_EXE.exists():
            self.start_shell()
        else:
            if self.use_shell:
                log.warning("the desktop shell isn't installed (run scripts\\setup.ps1)")
            print(f"Open the face in a browser: {self.url}", flush=True)
        await self._apply_realtime()                   # connects in the background
        self.transcript.start()
        await self.phone.start()
        try:                                           # the classic voice: the fallback (or the voice itself)
            await self.loop.run_in_executor(None, self._load_voice)
        except models.MissingModels as e:
            log.warning("no voice: %s", e)
            self.voice_error = str(e)
            if not self._realtime_wanted():
                self.notify(self.voice_error)
        except Exception as e:
            log.exception("no voice")
            self.voice_error = f"No voice: {e}"
            if not self._realtime_wanted():
                self.notify(self.voice_error)
        self.starting = False
        if self.profile.load_error:
            self.notify(self.profile.load_error)
        self._route_mic()                              # in a room the room listens
        self.changed()
        try:
            started = time.monotonic()
            while True:
                await asyncio.sleep(0.5)
                if self.shell_proc and self.shell_proc.poll() is not None:
                    if self.shell_proc.returncode and time.monotonic() - started < 5:
                        log.error("the desktop shell exited at once (code %s): see %s",
                                  self.shell_proc.returncode, self.profile.logs / "shell.log")
                    break                              # the face was closed: we're done
        finally:
            self.hearing.stop()
            for t in tasks:
                t.cancel()
            if self.realtime:
                await self.realtime.stop()
            await self.transcript.stop()
            await self.judge.close()
            await self.phone.stop()
            await self.room.stop()
            self.backend.shutdown()                    # the task that runs it closes it
            if self._backend_task:
                try:
                    await asyncio.wait_for(self._backend_task, 10)
                except Exception:
                    log.exception("stopping the agent")
            if self.voice:
                self.voice.close()
            if self.shell_proc and self.shell_proc.poll() is None:
                self.shell_proc.terminate()
            await runner.cleanup()
