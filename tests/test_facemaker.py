"""A face made to order: the agent's set_face tool has a portrait drawn (facemaker.py, by wireface.dev's portrait
service), the face page finds its landmarks, and the face wears it. The service is stood in for by a local server; the
face page by the test."""
import asyncio
import base64

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wireface import config, facemaker, profile as profiles, server
from wireface.activity import ActivityLog
from wireface.backends.claude import FACE_SET_TOOL, ClaudeBackend, describe_tool
from wireface.facemaker import FaceMakerError
from wireface.version import VERSION

LANDMARKS = [[0.5, 0.5]] * 468


# --- drawing it ------------------------------------------------------------------------------------------------------
def test_the_description_is_one_line_the_service_takes():
    assert facemaker.clean("  Ada \n Lovelace. ") == "Ada Lovelace."
    assert len(facemaker.clean("x" * 5000)) == facemaker.MAX_DESCRIPTION
    with pytest.raises(FaceMakerError):
        facemaker.clean("   ")


def test_it_asks_wireface_dev_for_a_portrait(monkeypatch):
    asked = []
    answers = {
        "a pirate": web.json_response({"image": base64.b64encode(b"\xff\xd8pirate").decode(), "mimeType": "image/jpeg",
                                       "model": "klein"}),
        "a refusal": web.json_response({"error": "the portrait couldn't be drawn: Request moderated"}, status=422),
        "too many": web.Response(text="<html>429 Too Many Requests</html>", status=429, content_type="text/html"),
        "broken": web.Response(text="<html>502 Bad Gateway</html>", status=502, content_type="text/html"),
        "nothing": web.json_response({"mimeType": "image/jpeg"}),
    }

    async def portrait(request):
        body = await request.json()
        asked.append((request.headers.get("X-Wireface-Client"), body))
        return answers[body["description"]]

    async def go():
        site = web.Application()
        site.router.add_post("/api/portrait", portrait)
        async with TestServer(site) as srv:
            monkeypatch.setattr(facemaker, "URL", str(srv.make_url("/api/portrait")))
            assert await facemaker.draw(" a  pirate ") == (b"\xff\xd8pirate", "image/jpeg")
            for description, says in (("a refusal", "Request moderated"), ("too many", "wait a minute"),
                                      ("broken", "answered 502"), ("nothing", "no picture")):
                with pytest.raises(FaceMakerError, match=says):
                    await facemaker.draw(description)
            monkeypatch.setattr(facemaker, "URL", "http://127.0.0.1:9/api/portrait")       # nothing listens there
            with pytest.raises(FaceMakerError, match="can't reach wireface.dev"):
                await facemaker.draw("a pirate")

    asyncio.run(go())
    assert asked[0] == (f"desktop/{VERSION}", {"description": "a pirate"})
    assert asked[1][1] == {"description": "a refusal"}
    assert config.PORTRAIT_URL == "https://wireface.dev/api/portrait"


# --- wearing it --------------------------------------------------------------------------------------------------------
@pytest.fixture
def app(tmp_path, monkeypatch):
    from wireface.app import App

    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "DEFAULT_CWD", tmp_path)
    a = App(profiles.resolve("drawn"), shell=False)
    drawn = []

    async def draw(who):
        drawn.append(who)
        return b"\x89PNG drawn", "image/png"

    monkeypatch.setattr(facemaker, "draw", draw)
    a.drawn = drawn
    return a


async def next_drawn(q: asyncio.Queue) -> str:
    while (msg := await q.get())["type"] != "face_drawn":
        pass
    return msg["id"]


def test_the_face_page_maps_the_drawn_face_and_the_face_wears_it(app):
    async def go():
        q = asyncio.Queue()
        app.clients.add(q)
        before = app.face["photo"]["version"]
        asking = asyncio.create_task(app.make_face("John Cena"))
        job = await next_drawn(q)
        assert app.drawn == ["John Cena"] and app.drawn_face(job) == (b"\x89PNG drawn", "image/png")
        await app.drawn_face_mapped(job, b"\xff\xd8mapped", LANDMARKS, 1024, 1024)
        cfg, jpeg = await asking
        assert jpeg == b"\xff\xd8mapped" and cfg == app.face and cfg["photo"]["version"] != before
        assert cfg["photo"]["portrait"] is None and cfg["photo"]["w"] == 1024
        assert (app.profile.photo_dir / "photo.jpg").read_bytes() == b"\xff\xd8mapped"
        assert app.drawn_face(job) is None
        with pytest.raises(KeyError):                              # a second face window is too late
            await app.drawn_face_mapped(job, b"\xff\xd8again", LANDMARKS, 1, 1)

    asyncio.run(go())


def test_when_the_face_page_cant_find_the_face_the_agent_hears_why(app):
    async def go():
        q = asyncio.Queue()
        app.clients.add(q)
        before = app.face
        asking = asyncio.create_task(app.make_face("a cloud"))
        await app.drawn_face_mapped(await next_drawn(q), None, [], 0, 0, "I couldn't find a face in that picture")
        with pytest.raises(FaceMakerError, match="couldn't find a face"):
            await asking
        assert app.face == before

    asyncio.run(go())


def test_it_needs_a_face_window_to_put_it_on(app):
    async def go():
        with pytest.raises(FaceMakerError, match="face window isn't open"):
            await app.make_face("a pirate")
        assert app.drawn == []                               # nothing is drawn that can't be put on
        app.clients.add(asyncio.Queue())
        app.MAP_TIMEOUT_S = 0.05
        with pytest.raises(FaceMakerError, match="didn't put it on"):        # open, but nobody maps it
            await app.make_face("a pirate")
        assert app._drawn == {}

    asyncio.run(go())


def test_the_api_hands_the_picture_to_the_page_and_takes_its_landmarks(app):
    jpeg = b"\xff\xd8\xff\xe0 a drawn face"
    found = {"image": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode(), "landmarks": LANDMARKS,
             "w": 1024, "h": 1024}

    async def go():
        async with TestClient(TestServer(server.build(app))) as client:
            auth = {"X-Face-Token": app.token}
            q = asyncio.Queue()
            app.clients.add(q)

            asking = asyncio.create_task(app.make_face("a pirate"))
            job = await next_drawn(q)
            assert (await client.get(f"/api/face/drawn/{job}")).status == 401          # the token, as everywhere
            r = await client.get(f"/api/face/drawn/{job}", headers=auth)
            assert r.status == 200 and r.content_type == "image/png" and await r.read() == b"\x89PNG drawn"
            r = await client.post(f"/api/face/drawn/{job}", json=found, headers=auth)
            assert r.status == 200 and (await r.json())["config"] == app.face
            assert (await asking)[1] == jpeg and (app.profile.photo_dir / "photo.jpg").read_bytes() == jpeg
            assert (await client.get(f"/api/face/drawn/{job}", headers=auth)).status == 404
            assert (await client.post(f"/api/face/drawn/{job}", json=found, headers=auth)).status == 404

            asking = asyncio.create_task(app.make_face("a fog bank"))
            r = await client.post(f"/api/face/drawn/{await next_drawn(q)}", headers=auth,
                                  json={"error": "I couldn't find a face in that picture"})
            assert r.status == 200
            with pytest.raises(FaceMakerError, match="couldn't find a face"):
                await asking

            asking = asyncio.create_task(app.make_face("a pirate"))         # a bad answer fails it at once
            r = await client.post(f"/api/face/drawn/{await next_drawn(q)}", json={**found, "image": "x"}, headers=auth)
            assert r.status == 400
            with pytest.raises(FaceMakerError, match="JPEG"):
                await asking

            asking = asyncio.create_task(app.make_face("a pirate"))         # and so do landmarks that don't fit
            r = await client.post(f"/api/face/drawn/{await next_drawn(q)}", json={**found, "landmarks": LANDMARKS[:9]},
                                  headers=auth)
            assert r.status == 400
            with pytest.raises(FaceMakerError, match="468"):
                await asking

    asyncio.run(go())


# --- the agent's tool ----------------------------------------------------------------------------------------------------
class Host:
    def __init__(self, tmp_path):
        self.backend_cfg = {**profiles.defaults()["backend"], "cwd": str(tmp_path)}
        self.state = {"session_id": None}
        self.activity = ActivityLog()
        self.worn, self.asked = [], []
        self.fails = None

    def speak(self, text): pass
    def notify(self, text): pass
    def changed(self): pass
    def save_state(self): pass
    def express(self, name, seconds=4): return name
    def show_face(self, visible): pass

    async def make_face(self, who):
        self.asked.append(who)
        if self.fails:
            raise FaceMakerError(self.fails)
        return {"photo": {}}, b"\xff\xd8face"

    async def use_portrait(self, pid):
        if pid not in ("ruth", "sarah"):
            raise ValueError(f"no portrait {pid!r}")
        self.worn.append(pid)


def test_claude_code_can_draw_itself_a_face_without_asking(tmp_path):
    options = ClaudeBackend(Host(tmp_path))._options()
    assert FACE_SET_TOOL in options.allowed_tools and FACE_SET_TOOL in options.system_prompt["append"]
    # it's told the face is the user's choice, and only who: nothing about pictures being made of anyone
    prompt = options.system_prompt["append"]
    assert "their choice" in prompt and not any(w in prompt.lower() for w in ("portrait is drawn", "likeness", "real person"))
    assert describe_tool(FACE_SET_TOOL, {"who": "a pirate"}) == "drawing a new face"
    assert describe_tool(FACE_SET_TOOL, {"portrait": "sarah"}) == "putting on a face"


def test_the_face_tool_draws_a_face_and_shows_it_to_the_agent(tmp_path):
    host = Host(tmp_path)
    backend = ClaudeBackend(host)
    backend._face_server()
    run = lambda args: asyncio.run(backend.set_face_tool.handler(args))

    made = run({"who": "a pirate captain"})
    assert host.asked == ["a pirate captain"] and not made.get("is_error")
    text, image = made["content"]
    assert image == {"type": "image", "data": base64.b64encode(b"\xff\xd8face").decode(), "mimeType": "image/jpeg"}
    run({"who": "John Cena"})
    assert host.asked[-1] == "John Cena"

    worn = run({"portrait": "Ruth"})
    assert host.worn == ["ruth"] and "Ruth" in worn["content"][0]["text"] and len(host.asked) == 2
    assert run({"portrait": "nobody"}).get("is_error") and run({}).get("is_error")

    host.fails = "too many new faces in a row: wait a minute and try again"
    failed = run({"who": "a robot"})
    assert failed.get("is_error") and "wait a minute" in failed["content"][0]["text"]
