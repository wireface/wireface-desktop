"""The room: one small process that several faces connect to over a local WebSocket.

It owns the microphone and speech recognition (so only the user is heard, once), decides who the user is talking
to, runs the turn rules (``scheduler.py``) and carries them out by messaging the faces. The faces keep their own
agents, voices and windows; they only report (what they say, when they have finished speaking) and obey (answer
this, you may speak now, stop).

Face -> room                                   Room -> face
  hello {name, kind, aliases, listening}         welcome {id, name}
  claim {round}                                  ask {round, floor, prompt}   contribute {round, prompt}
  reply {round, text, passed}                    final {round, prompt}        floor {round}
  speech_done {}                                 stop {}                      answer {allow}
  speaking {state, text}                         notice {text}                room {...status}
  say {text}  (typed into that face)
  halt {}  listening {on}  floor_request {}  permission {pending}
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil
from aiohttp import WSMsgType, web

from .. import config
from ..profile import read_json_lenient, write_json
from . import envelope, names
from .scheduler import Ask, Contribute, Final, GrantFloor, Halt, Limits, Notice, Scheduler

log = logging.getLogger(__name__)

IDLE_EXIT_S = 15.0
QUIET_AFTER_SPEECH_S = 0.8        # the room still rings for a moment after a voice stops
SPOKEN_WINDOW_S = 30.0            # how far back the words just spoken are compared with what the microphone hears
DIRECT_WINDOW_S = 20.0            # after "Codex", how long its question may take to follow
YES = re.compile(r"^\W*(yes|yeah|yep|sure|ok|okay|allow|go ahead|do it|please do)\b", re.I)
NO = re.compile(r"^\W*(no|nope|don't|do not|deny|stop|cancel)\b", re.I)


def rooms_dir() -> Path:
    return config.DATA / "rooms"


def room_file(name: str) -> Path:
    return rooms_dir() / f"{name}.json"


@dataclass
class Member:
    id: str
    name: str
    kind: str
    aliases: list
    out: asyncio.Queue = field(default_factory=asyncio.Queue)
    speaking: bool = False
    spoke_at: float = 0.0
    permission: bool = False


class Hub:
    def __init__(self, name: str, token: str | None = None, lead: str = "first", limits: Limits = Limits(),
                 clock=time.monotonic, make_hearing=None, debug: bool = False):
        self.name = name
        self.token = token or secrets.token_urlsafe(24)
        self.clock = clock
        self.debug = debug
        self.sched = Scheduler(lead=lead, limits=limits, clock=clock)
        self.members: dict[str, Member] = {}
        self.listening = False
        self.hearing = None
        self._make_hearing = make_hearing                # a factory(on_text, on_error) -> Hearing-like, or None
        self._spoken: collections.deque[tuple[float, str]] = collections.deque()
        self._pending_direct: tuple[str, float] | None = None
        self._ids = 0
        self._empty_since: float | None = self.clock()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.log_lines: list[str] = []

    # --- the faces ---
    def _alias_map(self) -> dict[str, list[str]]:
        return {m.id: m.aliases for m in self.members.values()}

    def _send(self, member: Member | str | None, obj: dict) -> None:
        targets = list(self.members.values()) if member is None else [self.members[member] if isinstance(member, str) else member]
        for m in targets:
            m.out.put_nowait(obj)

    def _status(self) -> dict:
        return {"type": "room", "listening": self.listening, "room": self.name, **self.sched.status()}

    def _others(self, member_id: str) -> list[str]:
        return [m.name for m in self.members.values() if m.id != member_id]

    # --- doing what the scheduler says ---
    def apply(self, acts) -> None:
        r = self.sched.round
        for a in acts:
            if isinstance(a, Ask) and a.member in self.members:
                lines = self.sched.transcript.take(a.member)
                prompt = envelope.ask_prompt(self.sched.name(a.member), self._others(a.member), lines,
                                             first_to_answer=not (r and r.exclusive))
                self._send(a.member, {"type": "ask", "round": a.round, "floor": a.floor, "prompt": prompt})
            elif isinstance(a, Contribute) and a.member in self.members:
                lines = self.sched.transcript.take(a.member)
                self._send(a.member, {"type": "contribute", "round": a.round,
                                      "prompt": envelope.contribute_prompt(self.sched.name(a.member), self._others(a.member), lines)})
            elif isinstance(a, Final) and a.member in self.members:
                lines = self.sched.transcript.take(a.member)
                self._send(a.member, {"type": "final", "round": a.round,
                                      "prompt": envelope.final_prompt(self.sched.name(a.member), self._others(a.member), lines)})
            elif isinstance(a, GrantFloor) and a.member in self.members:
                self._send(a.member, {"type": "floor", "round": a.round})
            elif isinstance(a, Halt):
                if a.member is None or a.member in self.members:
                    self._send(a.member, {"type": "stop"})
                if a.member is None:
                    self._spoken.clear()
            elif isinstance(a, Notice):
                log.info("room %s: %s", self.name, a.text)
                self._send(None, {"type": "notice", "text": a.text})
        self._send(None, self._status())

    # --- from the faces ---
    def hello(self, name: str, kind: str, aliases: list, listening: bool) -> Member:
        taken = {m.name.lower() for m in self.members.values()}
        unique, n = name, 2
        while unique.lower() in taken:
            unique, n = f"{name} {n}", n + 1
        self._ids += 1
        member = Member(f"m{self._ids}", unique, kind, names.aliases_for(unique, kind, aliases))
        self.members[member.id] = member
        self._empty_since = None
        self.sched.join(member.id, unique)
        self._send(member, {"type": "welcome", "id": member.id, "name": unique, "room": self.name})
        if listening and not self.listening:
            self.set_listening(True)
        self._send(None, self._status())
        return member

    def bye(self, member: Member) -> None:
        if self.members.pop(member.id, None) is None:
            return
        self.apply(self.sched.leave(member.id))
        if not self.members:
            self._empty_since = self.clock()

    def on_message(self, member: Member, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "claim":
            self.apply(self.sched.claim(member.id, msg.get("round")))
        elif kind == "reply":
            self.apply(self.sched.reply(member.id, str(msg.get("text") or ""), bool(msg.get("passed")), msg.get("round"),
                                        msg.get("kind")))
        elif kind == "speech_done":
            member.speaking, member.spoke_at = False, self.clock()
            self.apply(self.sched.speech_done(member.id))
        elif kind == "speaking":
            if msg.get("state") == "start":
                member.speaking = True
                if msg.get("text"):
                    self._spoken.append((self.clock(), str(msg["text"])))
            else:
                member.speaking, member.spoke_at = False, self.clock()
        elif kind == "floor_request":
            self.apply(self.sched.floor_request(member.id))
        elif kind == "say":
            self.user_text(str(msg.get("text") or ""), directed=member.id)
        elif kind == "halt":
            self.apply(self.sched.interrupt())
        elif kind == "listening":
            self.set_listening(bool(msg.get("on")))
        elif kind == "permission":
            member.permission = bool(msg.get("pending"))

    # --- the user ---
    def set_listening(self, on: bool) -> None:
        self.listening = on
        if self._make_hearing is None:
            self._send(None, self._status())
            return
        try:
            if on:
                if self.hearing is None:
                    self.hearing = self._make_hearing(self._heard_thread, self._hearing_error)
                self.hearing.start()
            elif self.hearing:
                self.hearing.stop()
        except Exception as e:
            log.exception("microphone")
            self._hearing_error(f"I can't listen: {e}")
            return
        self._send(None, self._status())

    def _heard_thread(self, text: str, info: dict | None = None) -> None:     # from the listening thread
        if self.loop:
            self.loop.call_soon_threadsafe(self.hear, text)

    def _hearing_error(self, message: str) -> None:
        def tell():
            self.listening = False
            self._send(None, {"type": "notice", "text": message})
            self._send(None, self._status())
        if self.loop:
            self.loop.call_soon_threadsafe(tell)
        else:
            tell()

    def _anyone_speaking(self) -> bool:
        now = self.clock()
        return any(m.speaking or now - m.spoke_at < QUIET_AFTER_SPEECH_S for m in self.members.values())

    def _spoken_text(self) -> str:
        cutoff = self.clock() - SPOKEN_WINDOW_S
        while self._spoken and self._spoken[0][0] < cutoff:
            self._spoken.popleft()
        return " ".join(t for _, t in self._spoken)

    def hear(self, text: str) -> str:
        """The microphone heard ``text``. Returns what the room made of it: "echo", "ignored", "interrupt", "user"."""
        if not text.strip() or not self.members:
            return "ignored"
        if self._anyone_speaking():
            h = names.classify(text, self._spoken_text(), self._alias_map())
            if h.kind != "interrupt":
                log.info("room %s: not for us while speaking (%s): %r", self.name, h.kind, text[:60])
                return h.kind if h.kind == "echo" else "ignored"
            self.apply(self.sched.interrupt())
            if h.member and h.rest:
                self.user_text(h.rest, directed=h.member)
            elif h.member:
                self._pending_direct = (h.member, self.clock() + DIRECT_WINDOW_S)
            return "interrupt"
        self.user_text(text)
        return "user"

    def user_text(self, text: str, directed: str | None = None) -> None:
        """The user's words (spoken once the room is quiet, or typed into a face): answer a pending permission
        question, or start a round."""
        text = text.strip()
        if not text:
            return
        asking = next((m for m in self.members.values() if m.permission), None)
        if asking and directed is None:
            if YES.match(text):
                self._send(asking, {"type": "answer", "allow": True})
                return
            if NO.match(text):
                self._send(asking, {"type": "answer", "allow": False})
                return
        everyone = False
        if directed is None:
            d = names.parse_directed(text, self._alias_map())
            directed, everyone = d.member, d.everyone
            if (directed or everyone) and not d.text:             # just a name: its question comes next
                if directed:
                    self._pending_direct = (directed, self.clock() + DIRECT_WINDOW_S)
                return
            if directed is None and not everyone and self._pending_direct:
                member, until = self._pending_direct
                if self.clock() < until and member in self.members:
                    directed = member
            self._pending_direct = None
        self.apply(self.sched.user_says(text, directed, everyone))

    # --- time ---
    def tick(self) -> bool:
        """Time limits. Returns False when the room has been empty long enough to close."""
        self.apply_if(self.sched.tick())
        return not (self._empty_since is not None and self.clock() - self._empty_since > IDLE_EXIT_S)

    def apply_if(self, acts) -> None:
        if acts:
            self.apply(acts)

    # --- the server ---
    def build(self) -> web.Application:
        app = web.Application()

        async def ws_handler(request):
            if request.query.get("t") != self.token:
                raise web.HTTPUnauthorized()
            sock = web.WebSocketResponse(heartbeat=20, max_msg_size=4 * 1024 * 1024)
            await sock.prepare(request)
            member: Member | None = None
            pump = None

            async def writer(m: Member):
                while True:
                    await sock.send_str(json.dumps(await m.out.get()))

            try:
                async for raw in sock:
                    if raw.type != WSMsgType.TEXT:
                        continue
                    try:
                        msg = json.loads(raw.data)
                    except ValueError:
                        continue
                    if member is None:
                        if msg.get("type") != "hello":
                            continue
                        member = self.hello(str(msg.get("name") or "Agent")[:40], str(msg.get("kind") or ""),
                                            [str(a) for a in msg.get("aliases") or []][:10], bool(msg.get("listening")))
                        pump = asyncio.create_task(writer(member))
                        continue
                    try:
                        self.on_message(member, msg)
                    except Exception:
                        log.exception("handling %s", msg.get("type"))
            finally:
                if pump:
                    pump.cancel()
                if member:
                    self.bye(member)
            return sock

        async def say(request):                              # a way to speak to the room without a microphone
            if not self.debug or request.query.get("t") != self.token:
                raise web.HTTPNotFound()
            data = await request.json()
            return web.json_response({"result": self.hear(str(data.get("text", ""))) if data.get("heard", True)
                                      else (self.user_text(str(data.get("text", ""))) or "user")})

        async def status(request):
            if request.query.get("t") != self.token:
                raise web.HTTPUnauthorized()
            return web.json_response({**self._status(), "transcript": self.sched.transcript.lines[-20:],
                                      "members": [m.name for m in self.members.values()]})

        app.router.add_get("/room", ws_handler)
        app.router.add_post("/say", say)
        app.router.add_get("/status", status)
        return app

    async def serve(self, port: int = 0) -> tuple[web.AppRunner, int]:
        self.loop = asyncio.get_running_loop()
        runner = web.AppRunner(self.build(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        return runner, runner.addresses[0][1]


# --- running as a process --------------------------------------------------------------------------------------------
def _alive(pid: int) -> bool:
    try:
        return any(a == "wireface" or a.startswith("wireface.") for a in psutil.Process(pid).cmdline())
    except psutil.Error:
        return False


def discover(name: str) -> dict | None:
    """The running room called ``name``: {pid, port, token}, or None."""
    info = read_json_lenient(room_file(name))
    if info.get("port") and info.get("token") and isinstance(info.get("pid"), int) and _alive(info["pid"]):
        return info
    return None


async def run_room(name: str, lead: str = "first", debug: bool = False) -> int:
    rooms_dir().mkdir(parents=True, exist_ok=True)
    if discover(name):
        log.error("room %s is already running", name)
        return 1

    def make_hearing(on_text, on_error):
        from ..hearing import Hearing

        return Hearing(lambda: None, on_text, on_error, lambda: False)          # never deaf: the room's gate decides

    hub = Hub(name, lead=lead, make_hearing=make_hearing, debug=debug)
    runner, port = await hub.serve()
    write_json(room_file(name), {"pid": os.getpid(), "port": port, "token": hub.token, "name": name})
    log.info("room %s serving on 127.0.0.1:%d", name, port)
    try:
        while hub.tick():
            await asyncio.sleep(0.5)
        log.info("room %s is empty: closing", name)
    finally:
        if hub.hearing:
            hub.hearing.stop()
        room_file(name).unlink(missing_ok=True)
        await runner.cleanup()
    return 0
