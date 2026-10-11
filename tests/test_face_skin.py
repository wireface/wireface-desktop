"""The skin: the face always wears a photo over its wireframe, skin_amount of the way down. A fresh face wears the
default portrait, the server puts built-in portraits on by itself, and faces saved before (hologram, sculpt) are brought
up to date."""
import asyncio
import json

import pytest

from wireface import config, faceconfig, profile as profiles

LANDMARKS = [[0.5, 0.5]] * 468


def portrait_bytes(pid, ext="jpg"):
    return (faceconfig.PORTRAITS_DIR / f"{pid}.{ext}").read_bytes()


# --- the setting ------------------------------------------------------------------------------------------------
def test_skin_amount_runs_from_the_wireframe_to_the_skin():
    assert faceconfig.DEFAULTS["skin_amount"] == 1 and faceconfig.DEFAULTS["style"] == "photo"
    assert faceconfig.STYLES == ("photo",)
    assert faceconfig.validate({"skin_amount": 0})["skin_amount"] == 0
    assert faceconfig.validate({"skin_amount": "0.4"})["skin_amount"] == 0.4
    for bad in (-0.1, 1.01, "lots", None):
        with pytest.raises(ValueError):
            faceconfig.validate({"skin_amount": bad})


def test_the_old_styles_are_the_photo_style_now():
    holo = faceconfig.validate({"style": "hologram"})               # the wireframe: the skin slid all the way off
    assert holo["style"] == "photo" and holo["skin_amount"] == 0
    sculpt = faceconfig.validate({"style": "sculpt"}, {**faceconfig.DEFAULTS, "skin_amount": 0.5})
    assert sculpt["style"] == "photo" and sculpt["skin_amount"] == 0.5
    assert faceconfig.validate({"style": "photo"}, holo)["skin_amount"] == 0          # the slider is left alone


def test_jpeg_size():
    assert min(faceconfig.jpeg_size(portrait_bytes("sarah"))) > 0
    assert faceconfig.jpeg_size(b"not a picture") == (0, 0) and faceconfig.jpeg_size(b"\xff\xd8\xff") == (0, 0)


# --- loading: the default portrait, and faces saved before --------------------------------------------------------
def test_a_fresh_face_wears_the_default_portrait(tmp_path):
    path, photos = tmp_path / "face.json", tmp_path / "face"
    cfg = faceconfig.load(path, photos)
    assert faceconfig.DEFAULT_PORTRAIT == "sarah"
    assert cfg["style"] == "photo" and cfg["skin_amount"] == 1 and cfg["photo"]["portrait"] == "sarah"
    assert min(cfg["photo"]["w"], cfg["photo"]["h"]) > 0
    assert (photos / "photo.jpg").read_bytes() == portrait_bytes("sarah")
    assert json.loads((photos / "landmarks.json").read_text()) == json.loads(portrait_bytes("sarah", "json"))
    assert json.loads(path.read_text()) == cfg                     # saved: the next start finds it on
    assert faceconfig.load(path, photos) == cfg


@pytest.mark.parametrize("style, skin", [("hologram", 0.0), ("sculpt", 1.0)])
def test_an_old_face_is_brought_up_to_date(tmp_path, style, skin):
    path, photos = tmp_path / "face.json", tmp_path / "face"
    path.write_text(json.dumps({"style": style, "glow": 1.4, "colors": {"primary": "#22D3EE", "skin": "#c9a48e"},
                                "photo": None}))
    cfg = faceconfig.load(path, photos)
    assert cfg["style"] == "photo" and cfg["skin_amount"] == skin and cfg["glow"] == 1.4
    assert cfg["colors"]["primary"] == "#22d3ee" and cfg["photo"]["portrait"] == "sarah"
    saved = json.loads(path.read_text())
    assert saved["style"] == "photo" and saved["skin_amount"] == skin and saved["photo"] == cfg["photo"]


def test_a_photo_of_your_own_is_kept(tmp_path):
    path, photos = tmp_path / "face.json", tmp_path / "face"
    faceconfig.write_photo(photos, b"\xff\xd8mine", LANDMARKS)
    mine = {"version": "abcd1234", "w": 640, "h": 480}
    path.write_text(json.dumps({"style": "hologram", "photo": mine}))
    cfg = faceconfig.load(path, photos)
    assert cfg["photo"] == mine and cfg["skin_amount"] == 0        # an old hologram: its photo slid off, still there
    assert (photos / "photo.jpg").read_bytes() == b"\xff\xd8mine"


def test_a_photo_whose_files_are_gone_gives_way_to_the_default(tmp_path):
    path = tmp_path / "face.json"
    path.write_text(json.dumps({"style": "photo", "photo": {"version": "gone", "w": 1, "h": 1}}))
    assert faceconfig.load(path, tmp_path / "face")["photo"]["portrait"] == "sarah"


def test_a_face_json_that_cannot_be_read_is_left_for_mending(tmp_path):
    path = tmp_path / "face.json"
    path.write_text("{ not json")
    assert faceconfig.load(path, tmp_path / "face")["photo"]["portrait"] == "sarah"
    assert path.read_text() == "{ not json"


def test_without_portraits_there_is_no_photo(tmp_path, monkeypatch):
    monkeypatch.setattr(faceconfig, "PORTRAITS_DIR", tmp_path / "none")
    assert faceconfig.load(tmp_path / "face.json", tmp_path / "face")["photo"] is None


def test_a_portrait_needs_its_landmarks(tmp_path, monkeypatch):
    (tmp_path / "ann.jpg").write_bytes(portrait_bytes("sarah"))
    monkeypatch.setattr(faceconfig, "PORTRAITS_DIR", tmp_path)
    (tmp_path / "portraits.json").write_text(json.dumps([{"id": "ann"}]))
    with pytest.raises(ValueError, match="landmarks"):
        faceconfig.wear_portrait("ann", tmp_path / "face")
    with pytest.raises(ValueError, match="no portrait"):
        faceconfig.wear_portrait("../ann", tmp_path / "face")
    with pytest.raises(ValueError):
        faceconfig.write_photo(tmp_path / "face", b"\xff\xd8", LANDMARKS[:100])
    assert not (tmp_path / "face").exists()


# --- the app and its API ---------------------------------------------------------------------------------------------
@pytest.fixture
def app(tmp_path, monkeypatch):
    from wireface.app import App

    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "DEFAULT_CWD", tmp_path)
    return App(profiles.resolve("skin"), shell=False)


def test_the_app_starts_with_the_default_portrait_on(app):
    assert app.face["photo"]["portrait"] == "sarah" and app.face["skin_amount"] == 1
    assert (app.profile.photo_dir / "photo.jpg").read_bytes() == portrait_bytes("sarah")
    assert json.loads(app.profile.face_file.read_text())["photo"] == app.face["photo"]


def test_a_portrait_is_put_on_by_the_server(app):
    sent = []
    app.broadcast = sent.append
    before = app.face["photo"]["version"]
    cfg = asyncio.run(app.use_portrait("ruth"))
    assert cfg["photo"]["portrait"] == "ruth" and cfg["photo"]["version"] != before
    assert (app.profile.photo_dir / "photo.jpg").read_bytes() == portrait_bytes("ruth")
    assert sent == [{"type": "face_config", "config": cfg}] and app.face == cfg
    with pytest.raises(ValueError):
        asyncio.run(app.use_portrait("nobody"))


def test_a_new_skin_comes_on_even_from_the_wireframe(app):
    asyncio.run(app.set_face({"skin_amount": 0}))
    assert asyncio.run(app.use_portrait("omar"))["skin_amount"] == 1                  # else it would not show
    asyncio.run(app.set_face({"skin_amount": 0.4}))
    assert asyncio.run(app.use_portrait("sarah"))["skin_amount"] == 0.4                 # partly on: left as it is
    assert asyncio.run(app.set_face({"style": "hologram"}))["skin_amount"] == 0       # the old way still works


def test_a_photo_of_your_own_and_back_to_the_default(app):
    cfg = asyncio.run(app.save_face_photo(b"\xff\xd8mine", LANDMARKS, 640, 480))
    assert cfg["photo"]["portrait"] is None and cfg["photo"]["w"] == 640 and cfg["style"] == "photo"
    assert asyncio.run(app.save_face_photo(b"\xff\xd8x", LANDMARKS, 1, 1, "walter"))["photo"]["portrait"] == "walter"
    assert asyncio.run(app.save_face_photo(b"\xff\xd8x", LANDMARKS, 1, 1, "../x"))["photo"]["portrait"] is None
    with pytest.raises(ValueError):
        asyncio.run(app.save_face_photo(b"\xff\xd8x", LANDMARKS[:10], 1, 1))
    cfg = asyncio.run(app.delete_face_photo())                    # no photo means the default portrait
    assert cfg["photo"]["portrait"] == "sarah" and (app.profile.photo_dir / "photo.jpg").read_bytes() == portrait_bytes("sarah")


def test_the_api_puts_a_portrait_on(app):
    from aiohttp.test_utils import TestClient, TestServer

    from wireface import server

    async def go():
        async with TestClient(TestServer(server.build(app))) as client:
            auth = {"X-Face-Token": app.token}
            r = await client.put("/api/face/photo", json={"portrait": "marcus"}, headers=auth)
            assert r.status == 200 and (await r.json())["config"]["photo"]["portrait"] == "marcus"
            r = await client.put("/api/face/photo", json={"portrait": "nobody"}, headers=auth)
            assert r.status == 400
            r = await client.put("/api/face", json={"skin_amount": 0.25}, headers=auth)
            assert (await r.json())["config"]["skin_amount"] == 0.25
            data = await (await client.get("/api/face", headers=auth)).json()
            assert data["styles"] == ["photo"] and data["defaults"]["skin_amount"] == 1
            assert data["portraits"] and all(p["landmarks"] for p in data["portraits"])

    asyncio.run(go())
