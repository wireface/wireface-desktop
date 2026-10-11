"""A public HTTPS address for the phone server, when the user hasn't given one: a Cloudflare quick tunnel.

``cloudflared tunnel --url http://127.0.0.1:PORT`` prints a random https://...trycloudflare.com address that reaches
that one local port (the phone server, never the face's own). It needs no account; the address changes each time, so
Wireface points the Twilio number at the new one when it starts (``phone.manage_number``).
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil

log = logging.getLogger(__name__)

ADDRESS = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def available() -> str | None:
    return shutil.which("cloudflared")


class Tunnel:
    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.url: str | None = None

    async def start(self, port: int, timeout_s: float = 40) -> str:
        exe = available()
        if not exe:
            raise RuntimeError("live calls need a public address: install cloudflared, or set Settings > Phone > "
                               "Public address")
        self.proc = await asyncio.create_subprocess_exec(
            exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            try:
                line = await asyncio.wait_for(self.proc.stderr.readline(), max(0.1, deadline - loop.time()))
            except asyncio.TimeoutError:
                break
            if not line:
                break
            m = ADDRESS.search(line.decode(errors="replace"))
            if m:
                self.url = m.group(0)
                asyncio.create_task(self._drain())
                log.info("phone: public address %s", self.url)
                return self.url
        await self.stop()
        raise RuntimeError("the Cloudflare tunnel did not start (is this PC online?)")

    async def _drain(self) -> None:
        """Keep reading its log, or it would block when the pipe fills."""
        while self.proc and self.proc.stderr and await self.proc.stderr.readline():
            pass

    async def stop(self) -> None:
        proc, self.proc, self.url = self.proc, None, None
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
