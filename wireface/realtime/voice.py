"""The face's voice in realtime mode: a speech model that speaks for the agent.

    you speak  ->  speech recognition on this PC -> your words go straight to the agent (Claude Code) as a prompt
                   and the realtime model gets a note of them (it never hears you, so it never answers you itself)
    agent works -> its activity log -> [activity] notes for the realtime model (silently)
    agent writes, asks, finishes, fails -> [say]: the realtime model tells you, in its own voice

The model speaks only when asked: so every request is answered once, by the agent, and passed on by the voice. It
still knows what the agent is doing at any moment: ask "how's it going?" while the agent is busy and it answers
from its notes (the question also waits for the agent, as everything you say does). The whole session (every tool
call, what the agent wrote, what the voice said) is in the activity panel.

``App`` owns one ``RealtimeVoice`` while ``voice.mode`` is "realtime". If the service can't be reached, the face
falls back to the classic voice (speech recognition and TTS on this PC) until it can.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from . import PROVIDERS as LINKS
from .audio import Speaker
from .brief import SAME, Narrator, clip, instructions, same_words
from .link import LinkError

log = logging.getLogger(__name__)

PUMP_S = 0.2
RESPONSE_WAIT_S = 6.0        # a [say] was sent: give its reply this long to start before sending another
USER_QUIET_S = 1.0           # don't start talking while the user talks, or just after: their next words may be coming
STABLE_S = 5.0               # a connection that lasted this long was fine (a reconnect after it is not a failure)


class RealtimeVoice:
    def __init__(self, app, stream: bool = True):
        self.app = app
        self.stream = stream                     # False in tests: no sound card
        self.narrator = Narrator(app.backend.label, self.cfg["narration"])
        self.speaker: Speaker | None = None
        self.link = None
        self.state = "off"                       # off | connecting | connected | retrying | failed
        self.error: str | None = None
        self.failures = 0                        # connection attempts that failed in a row
        self.responding = False                  # the model is speaking (or about to)
        self.user_at = 0.0                       # when the user was last heard (the microphone's voice detector)
        self._expect_until = 0.0
        self._muted = False                      # the reply in progress was cut off: drop the rest of it
        self._transcript = ""
        self._since_words = 0                    # samples of audio since the last transcript piece
        self._pieces: list[tuple[int, str]] = []    # (player index, words) of the reply being spoken
        self._heard_text: str | None = None      # a reply that was cut off: the words that came out before it
        self._asked: dict | None = None          # what the [say] in flight asked for (Narrator.last)
        self._speaking_for: dict | None = None   # ... and what the reply being spoken is for
        self._resume: str | None = None
        self._tasks: list[asyncio.Task] = []
        self._run_task: asyncio.Task | None = None
        self._last_error: tuple[str, float] = ("", 0.0)

    # --- settings ---
    @property
    def cfg(self) -> dict:
        return self.app.cfg["voice"]["realtime"]

    @property
    def provider(self) -> str:
        return self.cfg["provider"]

    @property
    def label(self) -> str:
        return LINKS[self.provider].label

    def _instructions(self) -> str:
        return instructions(self.app.cfg["name"], self.app.backend.label, self.cfg["persona"],
                            self.app.backend_cfg.get("cwd", ""))

    @property
    def handles_speech(self) -> bool:
        """Whether the agent's replies are this voice's to say (if not, the classic voice says them)."""
        return self.state == "connected" or (self.state == "connecting" and self.failures == 0)

    @property
    def speaking(self) -> bool:
        """Audible (or about to be). A reply that was cut off isn't, though the service may still be sending it (Gemini
        streams the rest of a turn it can't cancel): the microphone must not stay closed for that."""
        return bool((self.responding and not self._muted) or (self.speaker and self.speaker.busy))

    def view(self) -> dict:
        return {"state": self.state, "provider": self.provider, "label": self.label, "error": self.error,
                "speaking": self.speaking}

    # --- lifecycle ---
    async def start(self) -> None:
        if self._run_task:
            return
        loop = asyncio.get_running_loop()
        if self.speaker is None:
            on_mouth = lambda f: loop.call_soon_threadsafe(self.app.broadcast, {"type": "mouth", **f.to_json()})
            self.speaker = Speaker(LINKS[self.provider].output_rate, on_mouth=on_mouth, stream=self.stream)
        self.narrator.prime(self.app.activity.recent(300))
        self._run_task = asyncio.create_task(self._run(), name="realtime")
        self._tasks = [asyncio.create_task(self._pump(), name="realtime-pump")]

    async def stop(self) -> None:
        tasks = ([self._run_task] if self._run_task else []) + self._tasks
        self._run_task, self._tasks = None, []
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        link, self.link = self.link, None
        if link:
            await link.close()
        if self.speaker:
            self.speaker.close()
            self.speaker = None
        self.responding = False
        self.state = "off"

    async def restart(self) -> None:
        """Settings the session was opened with changed (service, model, voice, instructions)."""
        await self.stop()
        self._resume = None
        self.failures, self.error = 0, None
        self.narrator = Narrator(self.app.backend.label, self.cfg["narration"])
        await self.start()

    def set_level(self, level: str) -> None:
        self.narrator.level = level

    async def _run(self) -> None:
        delay = 1.0
        while True:
            cls = LINKS[self.provider]
            key, _source = self.app.credentials.realtime_key(self.provider)
            if not key:
                self._set("failed", f"There is no API key for {cls.label}: set {cls.key_envs[0]} or paste one in Settings")
                return
            p = self.cfg[self.provider]
            link = cls(key, p["model"], p["voice"], self._instructions(), self, resume_handle=self._resume)
            self._set("connecting" if self.failures == 0 else "retrying", self.error)
            try:
                await link.connect()
            except LinkError as e:
                log.warning("realtime voice: %s", e)
                if e.fatal:
                    self._set("failed", str(e))
                    return
                self.failures += 1
                self._set("retrying", str(e))
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            self.link = link
            self.failures = 0
            self._set("connected", None)
            log.info("realtime voice connected: %s %s (%s)", cls.label, link.model, "resumed" if link.resumed else "new")
            if not link.resumed:
                seed = self.narrator.seed()
                if seed:
                    await link.add_context(seed)
            began = time.monotonic()
            try:
                await link.run()
            finally:
                self.link = None
                self._resume = link.resume_handle
                await link.close()
                self.responding = False
            log.info("realtime voice disconnected: %s", link.closed_reason)
            if time.monotonic() - began < STABLE_S:          # it keeps dropping straight away: slow down
                self.failures += 1
                self._set("retrying", link.closed_reason)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
            else:
                delay = 1.0
                self._set("connecting", None)

    def _set(self, state: str, error: str | None) -> None:
        changed = (state, error) != (self.state, self.error)
        self.state, self.error = state, error
        if changed:
            self.app.realtime_changed()

    # --- the agent's side ---
    def activity(self, ev: dict) -> None:
        self.narrator.activity(ev, time.monotonic())

    def permission(self, show: str, say: str) -> None:
        self.narrator.permission(show, say)

    def typed(self, text: str) -> None:
        """The user typed to the agent: the voice should know, and stops talking."""
        self.narrator.typed(text)
        self.cut_off()

    def new_conversation(self) -> None:
        self.narrator.discard_speech()
        self.narrator.note(f"The user started a new conversation: {self.app.backend.label} no longer remembers the "
                           f"earlier requests.")

    def heard(self, text: str, queued: bool) -> None:
        """The user said this and it went to the agent (``queued``: behind work it is still doing)."""
        self.user_at = time.monotonic()
        self.narrator.user_said(text, queued, self.user_at)

    def user_voice(self) -> None:
        """The microphone hears someone talking: wait for them; with barge-in on, stop talking."""
        self.user_at = time.monotonic()
        if self.cfg["barge_in"] and self.speaking:
            self.cut_off()

    @property
    def deaf(self) -> bool:
        """Whether the microphone should ignore what it hears (the face is talking and would hear itself)."""
        return self.speaking and not self.cfg["barge_in"]

    def halt(self) -> None:
        """Stop talking now, and don't say what was waiting, nor what the request in progress still comes up with."""
        self.narrator.dismiss()
        self.cut_off()

    def cut_off(self) -> None:
        heard = self.speaker.heard_ms() if self.speaker else 0
        if self.speaker and self.responding and not self._muted:
            # the service sends the words ahead of the sound: keep only those that were heard
            at = self.speaker.playing_index()
            self._heard_text = "".join(t for i, t in self._pieces if at is not None and i < at).strip()
        if self.speaker:
            self.speaker.stop()
        link = self.link
        if link and link.responding:
            self._muted = True
            asyncio.create_task(link.cancel())
            asyncio.create_task(link.truncate(heard))

    # --- the pump: notes and speech requests go out when the time is right ---
    async def _pump(self) -> None:
        while True:
            await asyncio.sleep(PUMP_S)
            try:
                await self._step(time.monotonic())
            except Exception:
                log.exception("realtime pump")

    def _idle(self, now: float) -> bool:
        link = self.link
        # a muted reply doesn't block the next: a new request ends it (Gemini interrupts the old turn at once)
        busy = (link.responding or self.responding) and not self._muted if link else True
        return bool(link and not busy and not (self.speaker and self.speaker.busy)
                    and now - self.user_at >= USER_QUIET_S and now >= self._expect_until)

    async def _step(self, now: float) -> None:
        link = self.link
        if link is None:
            if not self.handles_speech:
                self.narrator.discard_speech()          # the classic voice says it meanwhile
            return
        ctx = self.narrator.take_context(now)
        if ctx:
            await link.add_context(ctx)
        if self._idle(now):
            say = self.narrator.take_speech(now)
            if say:
                ctx = self.narrator.take_context(now, force=True)
                if ctx:
                    await link.add_context(ctx)
                log.info("realtime voice asked to speak: %s", clip(say, 160))
                self._asked = self.narrator.last
                await link.respond(say)
                self._expect_until = now + RESPONSE_WAIT_S

    # --- events from the link (on the event loop) ---
    def on_ready(self) -> None:
        pass

    def on_response_start(self) -> None:
        self.responding = True
        self._muted = False
        self._transcript = ""
        self._pieces, self._heard_text = [], None
        self._expect_until = 0.0
        self._speaking_for, self._asked = self._asked, None
        if self.speaker:
            self.speaker.caption = None
        # the face takes a light mood cue from what it is about to say (the agent's own face tool still wins)
        self.app.event("actor.speaking", {"state": "start", "text": (self._speaking_for or {}).get("text", "")[:400]})
        self.app.changed()

    def on_audio(self, pcm: bytes) -> None:
        if not self._muted and self.speaker:
            self.speaker.write(pcm)
            self._since_words += len(pcm) // 2

    def on_transcript(self, text: str) -> None:
        if self._muted:
            return
        self._transcript += text
        if self.speaker:
            self.speaker.caption = caption_tail(self._transcript)
            self._pieces.append((self.speaker.words(text, self._since_words), text))
            self._since_words = 0

    def on_response_end(self, interrupted: bool) -> None:
        self.responding = False
        if self.speaker:
            self.speaker.end()
        cut = interrupted or self._muted
        text = re.sub(r"\s+", " ", self._heard_text if cut and self._heard_text is not None else self._transcript).strip()
        self._transcript, self._pieces, self._heard_text = "", [], None
        meta, self._speaking_for = self._speaking_for or {}, None
        if text or cut:
            extra = {"interrupted": cut}
            if meta.get("relays"):
                # which of the agent's replies this read out, and whether word for word (the panel shows it on them)
                extra.update(relays=meta["relays"], kind=meta["kind"],
                             same=meta["kind"] == "relay" and same_words(meta["text"], text) >= SAME)
            self.app.activity.emit("voice", name="face", turn_id=meta.get("turn") or self.narrator.turn,
                                   summary=clip(text, 300) or "(cut off before a word was heard)", output=text, extra=extra)
        self._muted = False
        self.narrator.spoke(time.monotonic())
        self.app.changed()

    def on_error(self, message: str) -> None:
        text, at = self._last_error
        now = time.monotonic()
        if message != text or now - at > 60:
            self._last_error = (message, now)
            self.app.notify(message)


def caption_tail(text: str, limit: int = 140) -> str:
    """The end of what is being said, for the caption: the current sentence or two."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    m = re.search(r"[.!?]\s+", cut)
    return cut[m.end():] if m and m.end() < len(cut) - 10 else cut[cut.find(" ") + 1:]
