"""The local web server behind the face page: static files, a small JSON API and one WebSocket.

Bound to 127.0.0.1 and guarded by a per-run token: whoever holds it can talk to a Claude Code session
that can run commands, so every /api and /ws request must carry it, and cross-site requests are refused.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import re

from aiohttp import WSMsgType, web

from . import catalog, config, faceconfig

log = logging.getLogger(__name__)

_MP = config.SHELL_TOOLS / "node_modules" / "@mediapipe" / "tasks-vision"
FACE_VENDOR = {
    "vision_bundle.mjs": _MP / "vision_bundle.mjs",
    **{f"wasm/{n}": _MP / "wasm" / n for n in ("vision_wasm_internal.js", "vision_wasm_internal.wasm",
                                                "vision_wasm_nosimd_internal.js", "vision_wasm_nosimd_internal.wasm",
                                                "vision_wasm_module_internal.js", "vision_wasm_module_internal.wasm")},
    "face_landmarker.task": config.LANDMARKER,
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def make_guard(token: str, get_port):
    """``get_port()``: the port in use (a face may be started on any free one, known once it is listening)."""

    @web.middleware
    async def guard(request, handler):
        path = request.path
        origins = {f"http://127.0.0.1:{get_port()}", f"http://localhost:{get_port()}"}
        if path.startswith("/api/") or path == "/ws":
            given = request.headers.get("X-Face-Token") or request.query.get("t", "")
            if not hmac.compare_digest(given, token):
                return web.json_response({"error": "unauthorised"}, status=401)
            origin = request.headers.get("Origin")
            if (request.method not in SAFE_METHODS or path == "/ws") and origin and origin not in origins:
                return web.json_response({"error": "bad origin"}, status=403)
            if request.method not in SAFE_METHODS and path != "/ws" and not origin:
                # browsers always send Origin on cross-site POSTs; require it so forms can't slip through
                if request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
                    return web.json_response({"error": "bad origin"}, status=403)
        resp = await handler(request)
        if path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        elif path.startswith("/static/"):
            # revalidate every load (a 304 when unchanged): with no header the browser guesses a freshness from
            # the file's age, and an edited script stays cached for hours
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    return guard


def json_ok(data=None, **kw):
    return web.json_response(data if data is not None else {"ok": True, **kw},
                             dumps=lambda o: json.dumps(o, default=str))


async def body(request) -> dict:
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def jpeg_from(data: dict) -> bytes:
    """The JPEG in a mapped photo's "image" (a data URL, as photo.js makes it); ValueError if it isn't one."""
    m = re.match(r"^data:image/jpeg;base64,([A-Za-z0-9+/=]+)$", str(data.get("image", "")))
    if not m:
        raise ValueError("expected a JPEG data URL")
    jpeg = base64.b64decode(m.group(1))
    if not jpeg.startswith(b"\xff\xd8") or len(jpeg) > 8 * 1024**2:
        raise ValueError("not a JPEG (or over 8 MB)")
    return jpeg


def build(A) -> web.Application:
    web_app = web.Application(middlewares=[make_guard(A.token, lambda: A.port)], client_max_size=12 * 1024**2)
    r = web_app.router

    # --- static ---------------------------------------------------------------------------------
    async def face_page(_):
        return web.FileResponse(config.WEB / "face.html", headers={"Cache-Control": "no-store"})

    async def face_vendor(request):
        """MediaPipe face landmarker (for mapping a photo onto the face), installed by scripts/setup.ps1."""
        target = FACE_VENDOR.get(request.match_info["path"])
        if not target or not target.is_file():
            raise web.HTTPNotFound()
        ctype = {".mjs": "text/javascript", ".js": "text/javascript", ".wasm": "application/wasm"}.get(
            target.suffix, "application/octet-stream")
        return web.FileResponse(target, headers={"Content-Type": ctype, "Cache-Control": "private, max-age=86400"})

    async def panel_page(_):
        return web.FileResponse(config.WEB / "panel.html", headers={"Cache-Control": "no-store"})

    r.add_get("/", face_page)
    r.add_get("/face", face_page)
    r.add_get("/panel", panel_page)
    r.add_get("/face-vendor/{path:.+}", face_vendor)
    r.add_static("/static", config.WEB, show_index=False)

    # --- talking to Claude --------------------------------------------------------------------------
    async def status(_):
        return json_ok(A.status())

    async def chat(request):
        text = str((await body(request)).get("text", "")).strip()
        if not text:
            return web.json_response({"error": "nothing to send"}, status=400)
        A.chat(text[:8000])
        return json_ok()

    async def halt(_):
        await A.halt()
        return json_ok()

    async def mode(request):
        try:
            await A.set_mode(await body(request))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return json_ok(A.status())

    async def session_new(_):
        A.new_conversation()
        return json_ok()

    async def session_compact(_):
        A.compact_conversation()
        return json_ok()

    async def permission(request):
        data = await body(request)
        done = A.answer_permission(bool(data.get("allow")), bool(data.get("always")), str(data.get("id") or ""))
        return json_ok(answered=done)

    async def activity(request):
        try:
            limit = int(request.query.get("limit", 500))
        except ValueError:
            limit = 500
        return json_ok({"events": A.activity.recent(min(max(limit, 1), 2000))})

    async def transcript(request):
        try:
            limit = int(request.query.get("limit", 400))
        except ValueError:
            limit = 400
        return json_ok(A.transcript.view(min(max(limit, 1), 400)))

    async def transcript_result(request):
        """A tool call's whole result from Claude Code's transcript (the activity log keeps only the start of it)."""
        found = await asyncio.get_running_loop().run_in_executor(None, A.transcript.tool_result,
                                                                 request.query.get("id", "")[:200])
        return json_ok({"found": bool(found), **(found or {})})

    # --- the agent's tools: MCP servers and skills (harness_tools.py) -------------------------------------------------
    async def tools_view(check: bool = False):
        if check and A.tools.stale():
            A.tools.refresh()                                 # in the background: the view says it's checking
        return await asyncio.get_running_loop().run_in_executor(None, A.tools.view)

    async def tools_get(request):
        return json_ok({"view": await tools_view(request.query.get("check") == "1")})

    async def tools_refresh(_):
        A.tools.refresh()
        return json_ok({"view": await tools_view()})

    async def tools_change(request, action):
        data = await body(request)
        try:
            said = await action(data)
        except (ValueError, asyncio.TimeoutError) as e:
            return web.json_response({"error": str(e) or "It took too long."}, status=400)
        except Exception as e:                              # the agent's own error (a failed toggle, say)
            log.exception("tools: %s", request.path)
            return web.json_response({"error": f"That didn't work: {e}"}, status=500)
        return json_ok({"message": said, "view": await tools_view()})

    async def tools_server(request):
        return await tools_change(request, lambda d: A.tools.set_server(str(d.get("key", "")), bool(d.get("enabled"))))

    async def tools_skill(request):
        return await tools_change(request, lambda d: A.tools.set_skill(str(d.get("key", "")), bool(d.get("enabled"))))

    async def tools_install(request):
        return await tools_change(request, lambda d: A.tools.install(str(d.get("id", ""))))

    async def tools_restart(request):
        async def go(_):
            await A.tools.restart()
            return f"Restarting {A.backend.label}."
        return await tools_change(request, go)

    async def speech_install(_):
        try:
            A.start_speech_install()
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=409)
        return json_ok()

    # --- voice ------------------------------------------------------------------------------------------
    async def eleven_voices(_):
        try:
            return json_ok({"voices": await A.elevenlabs_voices()})
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

    async def eleven_key(request):
        data = await body(request)
        key = data.get("key")
        try:
            await A.set_elevenlabs_key(None if key in (None, "") else str(key))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return json_ok(A.config_view())                       # says whether there is a key, never what it is

    async def realtime_key(request):
        data = await body(request)
        key = data.get("key")
        try:
            await A.set_realtime_key(str(data.get("provider") or ""), None if key in (None, "") else str(key))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return json_ok(A.config_view())                       # says whether there is a key, never what it is

    async def judge_key(request):
        data = await body(request)
        key = data.get("key")
        try:
            await A.set_judge_key(None if key in (None, "") else str(key))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return json_ok(A.config_view())                       # says whether there is a key, never what it is

    async def heard_send(request):
        """Send on something the judge kept back (the activity panel's Send button)."""
        if not A.send_held(str((await body(request)).get("id", ""))):
            return web.json_response({"error": "that is no longer waiting (it was sent, or is too old)"}, status=404)
        return json_ok()

    async def phone_token(request):
        data = await body(request)
        key = data.get("key")
        try:
            await A.set_phone_token(None if key in (None, "") else str(key))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return json_ok(A.config_view())                       # says whether there is a token, never what it is

    async def phone_agent(request):
        """A phone tool, for a harness that runs phone/mcp.py (which holds this face's token)."""
        data = await body(request)
        try:
            return json_ok(text=await A.phone_agent(str(data.get("tool", "")), data.get("args") or {}))
        except ValueError as e:
            return json_ok(error=str(e))

    async def phone_get(_):
        return json_ok(A.phone.panel_view())

    async def phone_conversation(request):
        from .phone.line import PhoneError
        try:
            return json_ok(A.phone.conversation_view(request.query.get("id", "")))
        except PhoneError as e:
            return web.json_response({"error": str(e)}, status=404)

    async def phone_test(_):
        try:
            return json_ok(message=await A.test_phone())
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=409)

    async def voice_test(_):
        try:
            return json_ok({"engine": await A.test_voice()})
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=409)

    # --- settings ---------------------------------------------------------------------------------------
    async def config_get(_):
        return json_ok(A.config_view())

    async def config_put(request):
        try:
            await A.set_config(await body(request))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return json_ok(A.config_view())

    # --- face ---------------------------------------------------------------------------------------
    async def face_get(_):
        return json_ok({"config": A.face, "presets": faceconfig.PRESETS, "defaults": faceconfig.DEFAULTS,
                        "portraits": faceconfig.portraits(), "expressions": list(faceconfig.EXPRESSIONS),
                        "styles": list(faceconfig.STYLES), "backgrounds": list(faceconfig.BACKGROUNDS),
                        "teeth_styles": list(faceconfig.TEETH_STYLES)})

    async def face_expression(request):
        data = await body(request)
        try:
            name = A.express(str(data.get("name", "")), data.get("seconds", 4))
        except (ValueError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return json_ok(name=name)

    async def models_get(request):
        """The models on offer for an agent (?backend=claude|codex|opencode, default the current one; &refresh=1)."""
        kind = request.query.get("backend") or A.backend_cfg["type"]
        loop = asyncio.get_running_loop()
        refresh = request.query.get("refresh") == "1"
        data, fetched, error = await loop.run_in_executor(None, catalog.catalog, refresh)
        if kind.startswith("realtime:"):                # ?backend=realtime:gemini: the realtime voice's models
            pid = kind.split(":", 1)[1]
            info = A.realtime_model_source(pid)
            if info is None:
                return web.json_response({"error": f"unknown realtime service {pid!r}"}, status=400)
            groups, error = await loop.run_in_executor(None, catalog.realtime_choices, pid, *info, data, refresh)
            return json_ok(backend=kind, groups=groups, fetched=fetched, error=error)
        served = None
        if kind == "opencode":                          # only the models OpenCode itself can serve
            served = await loop.run_in_executor(None, catalog.opencode_served, refresh)
        return json_ok(backend=kind, groups=catalog.choices(kind, data, served), fetched=fetched, error=error)

    async def face_visible(request):
        data = await body(request)
        A.show_face(bool(data.get("visible", True)))
        return json_ok(visible=A.face_visible)

    async def face_put(request):
        try:
            return json_ok({"config": await A.set_face(await body(request))})
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

    async def face_photo_get(_):
        path = A.profile.photo_dir / "photo.jpg"
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    async def face_landmarks_get(_):
        path = A.profile.photo_dir / "landmarks.json"
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-store", "Content-Type": "application/json"})

    async def face_photo_put(request):
        """{"portrait": id}: a built-in portrait; or {"image": JPEG data URL, "landmarks", "w", "h"}: a photo mapped in
        the page (with "portrait" too when it is one of those)."""
        data = await body(request)
        if "portrait" in data and "image" not in data:
            try:
                return json_ok({"config": await A.use_portrait(str(data["portrait"]))})
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=400)
        try:
            cfg = await A.save_face_photo(jpeg_from(data), data.get("landmarks") or [], int(data.get("w", 0)),
                                          int(data.get("h", 0)), data.get("portrait"))
        except (ValueError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return json_ok({"config": cfg})

    async def face_photo_delete(_):
        return json_ok({"config": await A.delete_face_photo()})

    async def face_drawn_get(request):
        """A face the agent asked for (its set_face tool), drawn and waiting for the face page to find its landmarks."""
        drawn = A.drawn_face(request.match_info["job"])
        if drawn is None:
            raise web.HTTPNotFound()
        image, mime = drawn
        return web.Response(body=image, content_type=mime)

    async def face_drawn_post(request):
        """What the face page found in it: {"image": JPEG data URL, "landmarks", "w", "h"}, or {"error": why not}.
        Anything wrong with it fails the request, so the agent hears at once instead of waiting it out."""
        data, job = await body(request), request.match_info["job"]
        bad = None
        try:
            if "error" in data:
                cfg = await A.drawn_face_mapped(job, None, [], 0, 0, str(data["error"])[:300] or "no reason given")
            else:
                try:
                    jpeg, w, h = jpeg_from(data), int(data.get("w", 0)), int(data.get("h", 0))
                except (ValueError, TypeError) as e:
                    bad = str(e)
                    await A.drawn_face_mapped(job, None, [], 0, 0, bad)
                else:
                    cfg = await A.drawn_face_mapped(job, jpeg, data.get("landmarks") or [], w, h)
        except KeyError:
            return web.json_response({"error": "no face is waiting for that"}, status=404)
        except (ValueError, TypeError) as e:
            bad = str(e)
        if bad:
            return web.json_response({"error": bad}, status=400)
        return json_ok({"config": cfg})

    # --- websocket -----------------------------------------------------------------------------------
    async def ws(request):
        sock = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024)
        await sock.prepare(request)
        q: asyncio.Queue = asyncio.Queue()
        A.clients.add(q)
        try:
            await sock.send_str(json.dumps({"type": "status", **A.status()}, default=str))

            async def pump():
                while True:
                    await sock.send_str(json.dumps(await q.get(), default=str))

            sender = asyncio.create_task(pump())
            try:
                async for msg in sock:
                    if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                        break
            finally:
                sender.cancel()
        finally:
            A.clients.discard(q)
        return sock

    for method, path, handler in (
        ("GET", "/api/status", status), ("POST", "/api/chat", chat), ("POST", "/api/halt", halt),
        ("POST", "/api/mode", mode), ("POST", "/api/session/new", session_new),
        ("POST", "/api/session/compact", session_compact),
        ("POST", "/api/permission", permission), ("GET", "/api/activity", activity),
        ("GET", "/api/transcript", transcript), ("GET", "/api/transcript/result", transcript_result),
        ("GET", "/api/tools", tools_get), ("POST", "/api/tools/refresh", tools_refresh),
        ("PUT", "/api/tools/server", tools_server), ("PUT", "/api/tools/skill", tools_skill),
        ("POST", "/api/tools/install", tools_install), ("POST", "/api/tools/restart", tools_restart),
        ("GET", "/api/config", config_get), ("PUT", "/api/config", config_put),
        ("POST", "/api/speech/install", speech_install),
        ("GET", "/api/voice/elevenlabs/voices", eleven_voices), ("PUT", "/api/voice/elevenlabs/key", eleven_key),
        ("PUT", "/api/voice/realtime/key", realtime_key), ("PUT", "/api/voice/judge/key", judge_key),
        ("POST", "/api/heard/send", heard_send),
        ("POST", "/api/voice/test", voice_test),
        ("PUT", "/api/phone/token", phone_token), ("POST", "/api/phone/test", phone_test),
        ("POST", "/api/phone/agent", phone_agent), ("GET", "/api/phone", phone_get),
        ("GET", "/api/phone/conversation", phone_conversation),
        ("GET", "/api/face", face_get), ("PUT", "/api/face", face_put), ("POST", "/api/face/expression", face_expression),
        ("POST", "/api/face/visible", face_visible),
        ("GET", "/api/models", models_get),
        ("GET", "/api/face/photo", face_photo_get), ("PUT", "/api/face/photo", face_photo_put),
        ("DELETE", "/api/face/photo", face_photo_delete), ("GET", "/api/face/landmarks", face_landmarks_get),
        ("GET", "/api/face/drawn/{job}", face_drawn_get), ("POST", "/api/face/drawn/{job}", face_drawn_post),
    ):
        r.add_route(method, path, handler)
    r.add_get("/ws", ws)
    return web_app


async def start(A) -> web.AppRunner:
    runner = web.AppRunner(build(A), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", A.profile.config["port"]).start()
    A.port = runner.addresses[0][1]                      # the port asked for, or the free one found for port 0
    log.info("serving %s on http://127.0.0.1:%d", A.profile.name, A.port)
    return runner
