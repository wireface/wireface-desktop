"""A face's side of a room: what it does with the room's messages, and what it tells the room.

While a face is in a room, what its agent says is not spoken straight away. The agent's turn is a *job* (an answer, a
comment, a conclusion), and the face speaks it only when the room has given it the floor:

    ask (floor: true)   addressed directly: it may speak at once.
    ask (floor: false)  racing the others: it holds what it says, tells the room it has something (claim), and
                        speaks when told it has the floor. If another agent got it, the answer stays unspoken.
    contribute / final  its turn, with the floor: it speaks (unless it says PASS, which is never spoken).

When the agent's turn ends, the face reports what it said (reply). After that, when its voice has gone quiet, it
tells the room the floor is free (speech_done). Only that releases the floor, so an agent keeps it through its
pauses, and two agents never speak at once.
"""
from __future__ import annotations

import asyncio
import collections
import logging
from dataclasses import dataclass, field

from . import envelope
from .client import RoomClient

log = logging.getLogger(__name__)


@dataclass
class Job:
    round: int | None
    kind: str                       # ask | contribute | final | loose (speech nobody asked for)
    floor: bool                     # may speak now
    buffer: list = field(default_factory=list)    # said, waiting to be spoken
    text: list = field(default_factory=list)      # everything said (what the others will read)
    claimed: bool = False
    cancelled: bool = False


class RoomLink:
    """``app`` is the App: ``cfg``, ``backend``, ``voice``, ``activity``, ``_speak_now``, ``halt``,
    ``answer_permission``, ``changed``, ``profile``."""

    def __init__(self, app, name: str | None, debug: bool = False):
        self.app = app
        self.name = name
        self.debug = debug
        self.client: RoomClient | None = None
        self.me: str | None = None                  # this face's name in the room (made unique by the room)
        self.status: dict = {}                      # the room's last status
        self.queue: collections.deque[Job] = collections.deque()    # prompts sent to the agent, in order
        self.current: Job | None = None             # the job whose turn is running
        self.finished: dict = {}                    # replied jobs that may still be given the floor, by round
        self.awaiting: Job | None = None            # replied and holding the floor: free it when the voice is quiet

    # --- joining ---
    @property
    def active(self) -> bool:
        return bool(self.client and self.client.connected)

    @property
    def joined(self) -> bool:
        return self.name is not None

    def start(self) -> None:
        if not self.name or self.client:
            return
        room = self.app.cfg["room"]
        self.client = RoomClient(
            self.name, self.app.cfg["name"], self.app.backend.id, list(room["aliases"]),
            bool(self.app.cfg["voice"]["listening"]), room["lead"], self.on_message, self._on_state, self.debug)
        self.client.start()

    async def stop(self) -> None:
        client, self.client = self.client, None
        if client:
            await client.stop()
        self._reset()
        self.status, self.me = {}, None

    async def rejoin(self, name: str | None) -> None:
        await self.stop()
        self.name = name
        self.start()
        self.app.changed()

    def _reset(self) -> None:
        for job in [self.current, *self.queue]:
            if job:
                job.cancelled = True
        self.queue.clear()
        self.finished.clear()
        self.awaiting = None

    def _on_state(self, up: bool) -> None:
        if not up:
            self.status = {}
            self._reset()
        self.app.changed()

    def send(self, **msg) -> None:
        if self.client:
            self.client.send(msg)

    # --- the room speaks ---
    def on_message(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "welcome":
            self.me = msg.get("name")
            self.app.changed()
        elif kind == "room":
            self.status = msg
            self.app.changed()
        elif kind in ("ask", "contribute", "final"):
            job = Job(msg.get("round"), kind, bool(msg.get("floor", kind != "ask")))
            self.queue.append(job)
            self.app.backend.send(str(msg.get("prompt") or ""))
            self.app.changed()
        elif kind == "floor":
            self._granted(msg.get("round"))
        elif kind == "stop":
            self._reset()
            asyncio.get_running_loop().create_task(self.app.halt(from_room=True))
        elif kind == "answer":
            self.app.answer_permission(bool(msg.get("allow")))
        elif kind == "notice":
            self.app.activity.emit("notice", summary=f"Room: {msg.get('text', '')}")

    def _granted(self, round_: int | None) -> None:
        job = self.current if self.current and self.current.round == round_ and not self.current.cancelled else None
        if job is None:
            job = self.finished.pop(round_, None)
            if job:
                self.awaiting = job
        if job is None:
            return
        job.floor = True
        self._flush(job)
        self.app.changed()

    def _flush(self, job: Job) -> None:
        while job.buffer:
            self.app._speak_now(job.buffer.pop(0))

    # --- the agent speaks ---
    def speak(self, text: str) -> bool:
        """A piece of the agent's reply. Returns False if it is not part of a job (the caller speaks it)."""
        job = self.current
        if job is None:
            return False
        if job.cancelled:
            return True                                  # an interrupted turn still finishing: say nothing
        job.text.append(text)
        job.buffer.append(text)
        if job.kind == "contribute" and envelope.might_become_pass(" ".join(job.text)):
            return True                                  # it may be a PASS: wait and see
        if job.floor:
            self._flush(job)
        elif not job.claimed:
            job.claimed = True
            if job.kind == "loose":
                self.send(type="floor_request")
            else:
                self.send(type="claim", round=job.round)
            self.app.changed()
        return True

    # --- the agent's turns ---
    def turn_started(self, source: str) -> None:
        if source == "user" and self.queue:
            self.current = self.queue.popleft()
        elif source == "user":
            self.current = None                          # a turn that is not the room's (a typed message in a browser...)
        else:
            self.current = Job(None, "loose", False)     # it started talking on its own (a background task finished)
        self.app.changed()

    def turn_done(self, source: str) -> None:
        job, self.current = self.current, None
        if job is None or job.cancelled:
            return
        text = " ".join(job.text).strip()
        passed = envelope.is_pass(text)
        if job.kind != "loose":
            self.send(type="reply", round=job.round, kind=job.kind, text=text, passed=passed)
        if passed:
            job.buffer.clear()
        elif job.floor:
            self._flush(job)
            self.awaiting = job
        else:
            self.finished[job.round] = job               # may still be given the floor (the claim is on its way)
        self.app.changed()

    def poll(self) -> None:
        """Called often: once the agent has finished and its voice has gone quiet, free the floor."""
        job = self.awaiting
        if job is None:
            return
        voice = self.app.voice
        if voice is not None and voice.busy and self.app.cfg["voice"]["on"]:
            return
        self.awaiting = None
        self.send(type="speech_done", round=job.round)

    # --- things that are the room's business now ---
    def say(self, text: str) -> None:
        """Typed into this face: the room starts a question addressed to this face's agent."""
        self.send(type="say", text=text)

    def set_listening(self, on: bool) -> None:
        self.send(type="listening", on=on)

    def halt(self) -> None:
        self.send(type="halt")

    def permission(self, pending: bool) -> None:
        self.send(type="permission", pending=pending)

    def speaking(self, state: str, text: str = "") -> None:
        self.send(type="speaking", state=state, text=text)

    # --- for the page ---
    @property
    def waiting(self) -> bool:
        """Has an answer it is holding back, waiting for its turn."""
        return bool(self.current and not self.current.floor and not self.current.cancelled and self.current.kind != "loose") or \
            any(j.buffer for j in self.finished.values())

    def view(self) -> dict | None:
        if not self.joined:
            return None
        s = self.status
        return {"name": self.name, "connected": self.active, "me": self.me, "listening": bool(s.get("listening")),
                "phase": s.get("phase", "idle"), "lead": s.get("lead"), "floor": s.get("floor"),
                "members": s.get("members", []), "waiting": self.waiting}
