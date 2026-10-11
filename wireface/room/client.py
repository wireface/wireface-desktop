"""A face's connection to its room. Finds the room process (starting it if this face is the first one in), joins,
and hands every message from the room to ``on_message``; if the room goes away it is found or started again.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from typing import Callable

import aiohttp

from .. import config
from . import hub

log = logging.getLogger(__name__)

SPAWN_WAIT_S = 20.0


def spawn_room(name: str, lead: str = "first", debug: bool = False) -> None:
    """Start the room process, detached: it outlives this face and closes itself when it has been empty a while."""
    argv = [sys.executable, "-m", "wireface.room", name, "--lead", lead] + (["--debug"] if debug else [])
    flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen(argv, cwd=str(config.ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)


async def find_or_start(name: str, lead: str = "first", debug: bool = False) -> dict:
    """{pid, port, token} of the room, starting it if nobody has. Two faces starting together start one room (the
    room refuses to run twice), and both then find it."""
    info = hub.discover(name)
    if info:
        return info
    hub.rooms_dir().mkdir(parents=True, exist_ok=True)
    marker = hub.rooms_dir() / f"{name}.starting"
    fresh = marker.exists() and time.time() - marker.stat().st_mtime < SPAWN_WAIT_S
    if not fresh:
        marker.write_text(str(os.getpid()))
        spawn_room(name, lead, debug)
    end = time.monotonic() + SPAWN_WAIT_S
    while time.monotonic() < end:
        await asyncio.sleep(0.2)
        info = hub.discover(name)
        if info:
            marker.unlink(missing_ok=True)
            return info
    raise RuntimeError(f"the room {name!r} did not start (see {hub.rooms_dir() / (name + '.log')})")


class RoomClient:
    """``on_message(dict)`` is called in the loop for each message; ``on_state(connected: bool)`` when the
    connection comes or goes. ``send`` is safe to call at any time (nothing is sent while disconnected)."""

    def __init__(self, room: str, name: str, kind: str, aliases: list[str], listening: bool, lead: str,
                 on_message: Callable[[dict], None], on_state: Callable[[bool], None] = lambda up: None, debug: bool = False):
        self.room, self.name, self.kind, self.aliases = room, name, kind, aliases
        self.listening, self.lead, self.debug = listening, lead, debug
        self.on_message, self.on_state = on_message, on_state
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task | None = None
        self._stop = False
        self.connected = False

    def start(self) -> None:
        self._stop = False
        self._task = asyncio.create_task(self._run(), name=f"room-{self.room}")

    async def stop(self) -> None:
        self._stop = True
        ws, self._ws = self._ws, None
        if ws is not None and not ws.closed:
            await ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def send(self, obj: dict) -> None:
        ws = self._ws
        if ws is not None and not ws.closed:
            asyncio.get_running_loop().create_task(self._send(ws, obj))

    async def _send(self, ws, obj: dict) -> None:
        try:
            await ws.send_str(json.dumps(obj))
        except Exception:
            log.debug("could not send to the room", exc_info=True)

    async def _run(self) -> None:
        delay = 1.0
        while not self._stop:
            try:
                info = await find_or_start(self.room, self.lead, self.debug)
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(f"ws://127.0.0.1:{info['port']}/room?t={info['token']}", heartbeat=20,
                                                  max_msg_size=4 * 1024 * 1024) as ws:
                        self._ws = ws
                        await ws.send_str(json.dumps({"type": "hello", "name": self.name, "kind": self.kind,
                                                      "aliases": self.aliases, "listening": self.listening}))
                        self.connected, delay = True, 1.0
                        self.on_state(True)
                        async for raw in ws:
                            if raw.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    self.on_message(json.loads(raw.data))
                                except Exception:
                                    log.exception("handling a message from the room")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("room %s: %s", self.room, e)
            finally:
                self._ws = None
                if self.connected:
                    self.connected = False
                    self.on_state(False)
            if not self._stop:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)
