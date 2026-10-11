"""Expressions (names, the API side, Claude Code's face tool), the portrait gallery, and per-request cost."""
import asyncio
import json

import pytest
from claude_agent_sdk import ResultMessage

from wireface import config, faceconfig, profile as profiles
from wireface.activity import ActivityLog
from wireface.backends.claude import FACE_TOOL, ClaudeBackend


def test_expressions_and_their_everyday_names():
    assert faceconfig.expression("Happy") == "happy"
    assert faceconfig.expression("fear") == "afraid" and faceconfig.expression("scared") == "afraid"
    assert faceconfig.expression(" surprise ") == "surprised"
    with pytest.raises(ValueError):
        faceconfig.expression("hungry")


def test_portraits_are_listed_with_their_images(tmp_path, monkeypatch):
    monkeypatch.setattr(faceconfig, "PORTRAITS_DIR", tmp_path)
    assert faceconfig.portraits() == []
    (tmp_path / "ann.jpg").write_bytes(b"x")
    (tmp_path / "ann-thumb.jpg").write_bytes(b"x")
    (tmp_path / "ann.json").write_text("[]")
    (tmp_path / "bob.jpg").write_bytes(b"x")
    (tmp_path / "portraits.json").write_text(json.dumps([{"id": "ann", "label": "Ann"}, {"id": "bob"},
                                                         {"id": "gone"}, {"id": "../x"}]))
    assert faceconfig.portraits() == [
        {"id": "ann", "label": "Ann", "image": "/static/face/portraits/ann.jpg", "thumb": "/static/face/portraits/ann-thumb.jpg",
         "landmarks": "/static/face/portraits/ann.json"},
        {"id": "bob", "label": "bob", "image": "/static/face/portraits/bob.jpg", "thumb": "/static/face/portraits/bob.jpg",
         "landmarks": None}]


def test_the_shipped_portraits_are_all_there_with_their_landmarks():
    shipped = faceconfig.portraits()
    assert len(shipped) >= 8 and all(p["thumb"].endswith("-thumb.jpg") for p in shipped)
    assert faceconfig.DEFAULT_PORTRAIT in [p["id"] for p in shipped]
    for p in shipped:                                  # made by scripts/portrait-landmarks.py
        assert p["landmarks"], f"{p['id']} has no landmarks: run scripts/portrait-landmarks.py"
        pts = json.loads((faceconfig.PORTRAITS_DIR / f"{p['id']}.json").read_text())
        assert len(pts) == 468 and all(len(xy) == 2 and 0 <= xy[0] <= 1 and 0 <= xy[1] <= 1 for xy in pts)
        jpeg = (faceconfig.PORTRAITS_DIR / f"{p['id']}.jpg").read_bytes()
        assert min(faceconfig.jpeg_size(jpeg)) >= 256


def test_the_app_puts_an_expression_on_the_face(tmp_path, monkeypatch):
    from wireface.app import App

    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(config, "DEFAULT_CWD", tmp_path)
    app = App(profiles.resolve("expr"), shell=False)
    sent = []
    app.event = lambda topic, payload=None: sent.append((topic, payload))
    assert app.express("scared", 99) == "afraid"
    assert sent == [("face.expression", {"name": "afraid", "seconds": 30.0})]       # held at most 30 s
    app.express("neutral")
    assert sent[-1] == ("face.expression", {"name": "neutral", "seconds": 0})
    with pytest.raises(ValueError):
        app.express("hungry")


class Host:
    def __init__(self, tmp_path):
        self.backend_cfg = {**profiles.defaults()["backend"], "cwd": str(tmp_path)}
        self.state = {"session_id": None}
        self.activity = ActivityLog()
        self.shown = []

    def speak(self, text): pass
    def notify(self, text): pass
    def changed(self): pass
    def save_state(self): pass
    async def ask_permission(self, show, say, can_always): return True, False

    def express(self, name, seconds=4):
        self.shown.append((name, seconds))
        return faceconfig.expression(name)


def test_claude_code_gets_the_face_tool_without_having_to_ask(tmp_path):
    options = ClaudeBackend(Host(tmp_path))._options()
    assert "face" in options.mcp_servers and FACE_TOOL in options.allowed_tools
    assert "mcp__face__express" in options.system_prompt["append"]


def test_the_face_tool_moves_the_face(tmp_path):
    host = Host(tmp_path)
    backend = ClaudeBackend(host)
    backend._face_server()
    result = asyncio.run(backend.face_tool.handler({"emotion": "fear", "seconds": 5}))
    assert host.shown == [("fear", 5)] and "afraid" in result["content"][0]["text"]
    bad = asyncio.run(backend.face_tool.handler({"emotion": "hungry"}))
    assert bad.get("is_error")


def test_each_request_shows_its_own_cost(tmp_path):
    host = Host(tmp_path)
    backend = ClaudeBackend(host)

    def result(total):
        backend._begin_turn("q", "user")
        backend._handle(ResultMessage(subtype="success", duration_ms=10, duration_api_ms=10, is_error=False, num_turns=1,
                                      session_id="s", total_cost_usd=total, usage={}))
        return next(e for e in reversed(host.activity.recent()) if e["kind"] == "turn")["extra"]

    assert result(0.36)["cost_usd"] == 0.36
    second = result(0.37)                                  # Claude Code reports the session's running total
    assert second["cost_usd"] == pytest.approx(0.01) and second["session_cost_usd"] == pytest.approx(0.37)
    third = result(0.05)                                   # a new CLI process starts its total again
    assert third["cost_usd"] == pytest.approx(0.05) and backend.session_cost == pytest.approx(0.42)


def test_claude_code_can_send_its_face_away_and_bring_it_back(tmp_path):
    from wireface.backends.claude import FACE_SHOW_TOOL

    host = Host(tmp_path)
    host.visible = []
    host.show_face = host.visible.append
    backend = ClaudeBackend(host)
    assert FACE_SHOW_TOOL in backend._options().allowed_tools and FACE_SHOW_TOOL in backend._options().system_prompt["append"]
    gone = asyncio.run(backend.show_face_tool.handler({"visible": False}))
    back = asyncio.run(backend.show_face_tool.handler({"visible": True}))
    assert host.visible == [False, True] and "gone" in gone["content"][0]["text"] and "back" in back["content"][0]["text"]
