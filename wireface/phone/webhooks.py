"""The phone server: the only part of Wireface that the internet can reach, and only for live calls.

It is a separate little web server (not the face's own, whose /api must never be public), on 127.0.0.1, published
through the public URL (``phone.public_url``, or a Cloudflare quick tunnel). It has three routes:

    POST /phone/voice    Twilio: a call is coming in. Answered with TwiML that connects its audio to /phone/stream
    POST /phone/status   Twilio: how a call Wireface placed went (no answer, busy, completed)
    GET  /phone/stream   Twilio's Media Stream for a call (WebSocket)

Both POSTs must carry a valid Twilio signature for exactly this URL. A stream is only accepted with a one-time token
(``Pending``) that Wireface itself put in the TwiML it gave Twilio, valid for a minute: the token decides who the call
is with and what the line agent does, so a stream can't be opened by anyone else or for anything else.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time

from aiohttp import web

from .twilio import valid_signature

log = logging.getLogger(__name__)

TOKEN_TTL_S = 60
START_TIMEOUT_S = 10
MAX_CALLS = 2               # at once


class Pending:
    """One-time tokens for call streams: token -> (what the call is, expiry)."""

    def __init__(self):
        self._items: dict[str, tuple[object, float]] = {}

    def issue(self, what, ttl: float = TOKEN_TTL_S) -> str:
        self._prune()
        token = secrets.token_urlsafe(24)
        self._items[token] = (what, time.monotonic() + ttl)
        return token

    def take(self, token: str | None):
        self._prune()
        item = self._items.pop(token or "", None)
        return item[0] if item else None

    def _prune(self) -> None:
        now = time.monotonic()
        for k in [k for k, (_, exp) in self._items.items() if exp < now]:
            del self._items[k]


def make_app(line) -> web.Application:
    """``line``: the PhoneLine (it decides what to answer, and runs the calls)."""

    async def signed(request: web.Request) -> dict | None:
        params = dict(await request.post())
        url = line.public_url + request.path_qs
        if not valid_signature(line.auth_token(), url, params, request.headers.get("X-Twilio-Signature")):
            log.warning("phone: refused a request to %s without a valid Twilio signature", request.path)
            return None
        return params

    async def voice(request: web.Request) -> web.Response:
        params = await signed(request)
        if params is None:
            return web.Response(status=403)
        twiml = await line.incoming_call(params)
        return web.Response(text=twiml, content_type="text/xml")

    async def status(request: web.Request) -> web.Response:
        params = await signed(request)
        if params is None:
            return web.Response(status=403)
        await line.call_status(params)
        return web.Response(status=204)

    async def stream(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=1 << 20)
        await ws.prepare(request)
        if len(line.calls) >= MAX_CALLS:
            await ws.close()
            return ws
        start = None
        try:
            async with asyncio.timeout(START_TIMEOUT_S):
                async for msg in ws:
                    data = json.loads(msg.data) if msg.type == web.WSMsgType.TEXT else {}
                    if data.get("event") == "start":
                        start = data.get("start") or {}
                        break
        except (TimeoutError, ValueError):
            pass
        what = line.pending.take(((start or {}).get("customParameters") or {}).get("token"))
        if not start or what is None:
            log.warning("phone: closed a call stream without a valid token")
            await ws.close()
            return ws
        await line.run_call(what, ws, start.get("streamSid", ""), start.get("callSid", ""))
        return ws

    app = web.Application(client_max_size=64 * 1024)
    app.router.add_post("/phone/voice", voice)
    app.router.add_post("/phone/status", status)
    app.router.add_get("/phone/stream", stream)
    return app


async def start(line, port: int = 0) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(make_app(line), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]
