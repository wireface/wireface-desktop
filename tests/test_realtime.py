"""The realtime voice: what it is told about the agent's work (brief.Narrator), the two services' protocols (against a
stand-in WebSocket server), and how RealtimeVoice and the App route speech, the microphone and permission questions."""
import asyncio
import base64
import json

import pytest
from aiohttp import WSMsgType, web

from wireface import config, credentials, profile as profiles
from wireface.activity import ActivityLog
from wireface.realtime import gemini, openai
from wireface.realtime.brief import ACK_S, BACKGROUND_WAIT_S, CONTEXT_EVERY_S, Narrator, instructions
from wireface.realtime.link import LinkError
from wireface.realtime.voice import RealtimeVoice, caption_tail


# --- what the voice is told ------------------------------------------------------------------------------------------
def ev(kind, id, status="ok", **fields):
    return {"kind": kind, "id": id, "status": status, "turn_id": fields.pop("turn_id", "t1"), "parent_id": None,
            "name": "", "summary": "", "input": None, "output": None, "duration_ms": None, "extra": {}, **fields}


def test_a_request_its_tools_and_its_reply_become_notes_and_one_thing_to_say():
    n = Narrator("Claude Code")
    n.activity(ev("turn", "t1", "running", input="fix the failing test", turn_id=None), 0)
    n.activity(ev("tool", "a", "running", name="Read", summary="app.py"), 1)
    n.activity(ev("tool", "a", "ok", name="Read", summary="app.py", output="...file..."), 1.2)
    n.activity(ev("tool", "b", "running", name="Bash", summary="pytest -q"), 2)
    n.activity(ev("tool", "b", "ok", name="Bash", summary="pytest -q", output="....\n42 passed in 3s\n"), 3)
    n.activity(ev("assistant", "c", "running", output="I fixed"), 4)
    assert n.take_speech(4) is None                                  # still being written
    n.activity(ev("assistant", "c", "ok", output="I fixed the off-by-one in app.py; all 42 tests pass."), 5)
    ctx = n.take_context(10)
    assert ctx.startswith("[activity]")
    assert 'started on the request: "fix the failing test"' in ctx and "used Read: app.py" in ctx
    assert "42 passed in 3s" in ctx and ctx.count("used Read") == 1     # an update of a step is not a new note
    say = n.take_speech(10)
    assert say.startswith("[say]") and "off-by-one" in say and n.take_speech(11) is None
    n.activity(ev("turn", "t1", "ok", turn_id=None, duration_ms=12000), 12)
    assert n.take_speech(12) is None                                  # it already passed on a reply for this request
    assert "finished that request after 12 s" in n.take_context(20)


def test_a_request_that_ends_without_a_reply_is_still_reported():
    n = Narrator("Codex")
    n.activity(ev("turn", "t1", "running", input="tidy up", turn_id=None), 0)
    n.activity(ev("tool", "a", "ok", name="shell", summary="git status"), 1)
    n.activity(ev("turn", "t1", "ok", turn_id=None), 2)
    assert "finished without writing a reply" in n.take_speech(3)


def test_quiet_holds_what_the_agent_writes_until_the_request_is_done():
    n = Narrator("Claude Code", "quiet")
    n.activity(ev("turn", "t1", "running", input="x", turn_id=None), 0)
    n.activity(ev("assistant", "a", output="Let me look at the tests first."), 1)
    assert n.take_speech(2) is None
    n.activity(ev("assistant", "b", output="Done: the tests pass."), 3)
    n.activity(ev("turn", "t1", "ok", turn_id=None), 4)
    say = n.take_speech(5)
    assert "Let me look" in say and "the tests pass" in say


def test_a_permission_question_is_asked_unless_it_was_answered_first():
    n = Narrator("Claude Code")
    n.activity(ev("permission", "p", "running", summary="Run: rm -rf build"), 0)
    n.permission("Run: rm -rf build", "Can I run a command?")
    say = n.take_speech(1)
    assert "rm -rf build" in say and "Can I run a command?" in say
    n.permission("Edit app.py", "Can I edit app.py?")
    n.activity(ev("permission", "p2", "ok", summary="Edit app.py"), 2)       # answered on the face first
    assert n.take_speech(3) is None


def test_progress_updates_follow_the_narration_level():
    n = Narrator("Claude Code", "normal")
    n.activity(ev("turn", "t1", "running", input="x", turn_id=None), 0)
    n.activity(ev("tool", "a", "running", name="Read", summary="a.py"), 1)
    assert n.take_speech(10) is None
    assert "progress update" in n.take_speech(26)
    assert n.take_speech(40) is None                                  # nothing new since the last update
    n.activity(ev("tool", "b", "running", name="Read", summary="b.py"), 41)
    assert n.take_speech(45) is None and n.take_speech(52) is not None
    q = Narrator("Claude Code", "quiet")
    q.activity(ev("turn", "t1", "running", input="x", turn_id=None), 0)
    q.activity(ev("tool", "a", "running", name="Read"), 1)
    assert q.take_speech(1000) is None


def test_problems_background_tasks_and_subagents():
    n = Narrator("Claude Code")
    n.activity(ev("error", "e", summary="Claude Code stopped: boom", turn_id=None), 0)
    assert "boom" in n.take_speech(1)
    n.activity(ev("task", "k", "running", summary="run the build", turn_id=None), 2)
    n.activity(ev("task", "k", "ok", summary="run the build", output="build ok", turn_id=None), 3)
    assert n.take_speech(4) is None                                   # the agent may report it itself
    assert "background task finished" in n.take_speech(3 + BACKGROUND_WAIT_S).lower()
    n.activity(ev("task", "k2", "running", summary="run the tests", turn_id=None), 20)
    n.activity(ev("task", "k2", "ok", summary="run the tests", turn_id=None), 21)
    n.activity(ev("turn", "bg", "running", turn_id=None, extra={"source": "background"}), 22)    # ... and it does
    n.activity(ev("assistant", "r", output="The tests finished: all green.", turn_id="bg"), 23)
    say = n.take_speech(40)
    assert "all green" in say and "background task" not in say.lower()
    n.activity(ev("assistant", "s", output="subagent detail", parent_id="tool1"), 5)
    assert n.take_speech(6) is None and "A subagent wrote" in n.take_context(9)


def test_the_voices_own_lines_and_old_events_are_never_said():
    n = Narrator("Claude Code")
    n.activity(ev("voice", "v", output="I said this"), 0)
    assert n.take_context(5, force=True) is None
    n.prime([ev("turn", "t0", "running", input="earlier", turn_id=None), ev("assistant", "a0", output="old reply"),
             ev("turn", "t0", "ok", turn_id=None)])
    assert n.take_speech(1) is None and n.take_context(5, force=True) is None
    assert "earlier" in n.seed() and "old reply" in n.seed()


def test_what_the_user_said_is_answered_by_the_agent_not_by_the_voice():
    n = Narrator("Claude Code")
    n.user_said("What's 4 plus 2?", queued=False, now=0)
    assert 'The user said to Claude Code: "What\'s 4 plus 2?"' in n.take_context(5)
    n.activity(ev("turn", "t1", "running", input="What's 4 plus 2?", turn_id=None), 0.1)
    assert n.take_speech(1) is None                                   # the voice says nothing of its own
    n.activity(ev("assistant", "a", output="Four plus two is six."), 2)
    say = n.take_speech(2.5)
    assert "Four plus two is six." in say and "on it" not in say.lower()
    n.activity(ev("turn", "t1", "ok", turn_id=None), 3)
    assert n.take_speech(20) is None                                  # no late "on it" either


def test_a_slow_answer_gets_a_short_on_it():
    n = Narrator("Claude Code", "normal")
    n.user_said("refactor the parser", queued=False, now=0)
    n.activity(ev("turn", "t1", "running", input="refactor the parser", turn_id=None), 0.1)
    n.activity(ev("tool", "a", "running", name="Read", summary="parser.py"), 1)
    assert n.take_speech(ACK_S["normal"] - 1) is None
    assert "on it" in n.take_speech(ACK_S["normal"] + 0.1).lower()
    assert n.take_speech(ACK_S["normal"] + 1) is None
    q = Narrator("Claude Code", "quiet")
    q.user_said("refactor the parser", queued=False, now=0)
    q.activity(ev("turn", "t1", "running", input="x", turn_id=None), 0.1)
    assert q.take_speech(60) is None


def test_while_the_agent_is_busy_the_voice_answers_once():
    n = Narrator("Claude Code")
    n.activity(ev("turn", "t1", "running", input="run the tests", turn_id=None), 0)
    n.activity(ev("tool", "a", "running", name="Bash", summary="pytest"), 1)
    n.user_said("how's it going?", queued=True, now=2)
    say = n.take_speech(3)
    assert "how's it going?" in say and "still busy" in say
    assert n.take_speech(10) is None
    n.activity(ev("assistant", "r1", output="All 42 tests pass."), 11)
    n.activity(ev("turn", "t1", "ok", turn_id=None), 12)
    assert "All 42 tests pass" in n.take_speech(13)
    # the question reaches the agent later: its answer is a follow-up, said only for what is new
    n.activity(ev("turn", "t2", "running", input="How's it going?", turn_id=None), 14)
    n.activity(ev("assistant", "r2", output="Going well, nothing is running now.", turn_id="t2"), 15)
    n.activity(ev("turn", "t2", "ok", turn_id=None), 16)
    say = n.take_speech(17)
    assert "you answered from your notes" in say and "only what is new" in say and "Going well" in say
    assert n.take_speech(30) is None


def test_an_answer_for_a_busy_agent_is_dropped_once_the_agent_gets_to_it():
    n = Narrator("Claude Code")
    n.activity(ev("turn", "t1", "running", input="weather in Scotland", turn_id=None), 0)
    n.user_said("What's the weather in France?", queued=True, now=1)
    # the voice was talking, so it never said "it's next"; meanwhile the agent finished and started on France
    n.activity(ev("turn", "t1", "ok", turn_id=None), 2)
    n.activity(ev("turn", "t2", "running", input="What's the weather in France?", turn_id=None), 2.1)
    say = n.take_speech(3)
    assert say is None or "it's next" not in say
    n.activity(ev("assistant", "a", output="Paris is about 20 degrees.", turn_id="t2"), 10)
    say = n.take_speech(11)
    assert "Paris is about 20 degrees." in say and "only what is new" not in say      # a normal reply, said in full


def test_after_the_user_cuts_in_the_old_request_is_not_read_out():
    n = Narrator("Claude Code")
    n.activity(ev("turn", "t1", "running", input="weather in Berlin", turn_id=None), 0)
    n.dismiss()                                                      # "Claude, ignore that..."
    n.activity(ev("assistant", "a", output="Berlin is 15 degrees.", turn_id="t1"), 1)
    n.activity(ev("turn", "t1", "ok", turn_id=None), 1.1)
    assert n.take_speech(2) is None and "Berlin is 15 degrees" in n.take_context(5, force=True)
    n.activity(ev("turn", "t2", "running", input="Rome instead", turn_id=None), 3)
    n.activity(ev("assistant", "b", output="Rome is 20 degrees.", turn_id="t2"), 4)
    assert "Rome is 20 degrees" in n.take_speech(5)


def test_a_reply_is_read_word_for_word_on_its_own():
    n = Narrator("Claude Code")
    n.activity(ev("turn", "t1", "running", input="x", turn_id=None), 0)
    n.permission("Run: npm test", "Can I run a command?")
    n.activity(ev("assistant", "a1", output="I'll run the tests now."), 1)
    say = n.take_speech(2)
    assert "word for word" in say and "I'll run the tests now." in say and "npm test" not in say
    assert n.last == {"relays": ["a1"], "turn": "t1", "text": "I'll run the tests now.", "kind": "relay"}
    ask = n.take_speech(3)                                            # the question comes next, in its own words
    assert "npm test" in ask and n.last["kind"] == "own" and n.last["relays"] == []
    n.activity(ev("assistant", "a2", output="Long. " * 300), 4)
    assert "sum up the rest" in n.take_speech(5)


def test_notes_go_out_in_small_batches_unless_forced():
    n = Narrator("Claude Code")
    n.note("one")
    assert n.take_context(100) is None                                 # the batch starts now
    assert n.take_context(100 + CONTEXT_EVERY_S / 2) is None
    assert n.take_context(100 + CONTEXT_EVERY_S + 0.01) =="[activity]\n- one"
    n.note("two")
    assert n.take_context(200, force=True) == "[activity]\n- two"


def test_instructions_name_the_agent_and_the_persona():
    me = instructions("Claude", "Claude Code", "self")
    assert "first person" in me and "You don't hear the user" in me and "Never answer the user's requests" in me
    relay = instructions("Claude", "Codex", "relay", "C:\\work")
    assert "Refer to it as Codex" in relay and "C:\\work" in relay
    assert "[activity]" in relay and "[say]" in relay


def test_caption_tail_keeps_the_end_of_what_is_said():
    assert caption_tail("Short.") == "Short."
    long = "First sentence here. " * 10 + "And the last one now."
    assert caption_tail(long).endswith("the last one now.") and len(caption_tail(long)) <= 140


# --- the services, against a stand-in server ----------------------------------------------------------------------------
class Events:
    def __init__(self):
        self.log = []

    def __getattr__(self, name):
        if not name.startswith("on_"):
            raise AttributeError(name)
        return lambda *a: self.log.append((name[3:], *a))


async def serve(handler):
    app = web.Application()

    async def ws(request):
        sock = web.WebSocketResponse()
        await sock.prepare(request)
        await handler(sock, request)
        return sock

    app.router.add_get("/ws", ws)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"ws://127.0.0.1:{runner.addresses[0][1]}/ws"


def b64(n):
    return base64.b64encode(b"\x01\x00" * n).decode()


def test_gemini_session(monkeypatch):
    got = {}

    async def server(sock, request):
        got["key"] = request.query.get("key")
        got["setup"] = json.loads((await sock.receive()).data)["setup"]
        await sock.send_str(json.dumps({"setupComplete": {}}))
        await sock.send_str(json.dumps({"sessionResumptionUpdate": {"newHandle": "h1", "resumable": True}}))
        await sock.send_str(json.dumps({"serverContent": {"modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": b64(4)}}]},
                                                          "outputTranscription": {"text": "On it."}}}))
        await sock.send_str(json.dumps({"serverContent": {"turnComplete": True}}))
        got["context"] = json.loads((await sock.receive()).data)
        got["respond"] = json.loads((await sock.receive()).data)
        await sock.close()

    async def go():
        runner, url = await serve(server)
        monkeypatch.setattr(gemini, "URL", url)
        events = Events()
        link = gemini.GeminiLink("k-123", "", "", "be brief", events)
        await link.connect()
        reader = asyncio.create_task(link.run())
        await asyncio.sleep(0.2)
        await link.add_context("[activity] note")
        await link.respond("[say] hi")
        await asyncio.wait_for(reader, 5)
        await link.close()
        await runner.cleanup()
        return link, events

    link, events = asyncio.run(go())
    s = got["setup"]
    assert got["key"] == "k-123" and s["model"] == "models/gemini-3.8-live"
    assert s["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Puck"
    assert s["systemInstruction"]["parts"][0]["text"] == "be brief" and s["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
    assert "inputAudioTranscription" not in s and "realtimeInputConfig" not in s       # it never hears the user
    names = [e[0] for e in events.log]
    assert names == ["ready", "response_start", "audio", "transcript", "response_end"]
    assert ("response_end", False) in events.log
    assert got["context"]["clientContent"]["turnComplete"] is False and got["respond"]["clientContent"]["turnComplete"] is True
    assert link.resume_handle == "h1"


def test_gemini_refusals(monkeypatch):
    attempts = []

    async def server(sock, request):
        setup = json.loads((await sock.receive()).data)["setup"]
        attempts.append(setup)
        if "thinkingConfig" in setup["generationConfig"]:
            await sock.close(code=1007, message=b"Thinking budget is not supported for this model.")
        elif setup["model"].endswith("nope"):
            await sock.close(code=1008, message=b"models/nope is not found for API version v1beta")
        else:
            await sock.send_str(json.dumps({"setupComplete": {}}))
            await sock.receive()

    async def go(model):
        runner, url = await serve(server)
        monkeypatch.setattr(gemini, "URL", url)
        link = gemini.GeminiLink("k", model, "Kore", "x", Events())
        try:
            await link.connect()
            return link
        finally:
            await link.close()
            await runner.cleanup()

    link = asyncio.run(go("gemini-3.8-live"))
    assert not link.thinking_off and len(attempts) == 2               # the setting was dropped and it tried again
    with pytest.raises(LinkError) as info:
        asyncio.run(go("nope"))
    assert info.value.fatal and "model" in str(info.value)


def test_openai_session(monkeypatch):
    got = {"sent": []}

    async def server(sock, request):
        got["auth"] = request.headers.get("Authorization")
        got["model"] = request.query.get("model")
        got["session"] = json.loads((await sock.receive()).data)
        await sock.send_str(json.dumps({"type": "session.updated", "session": {}}))
        for msg in ({"type": "response.created", "response": {"id": "r1"}},
                    {"type": "response.output_audio.delta", "item_id": "i1", "delta": b64(4)},
                    {"type": "response.output_audio_transcript.delta", "delta": "On it."},
                    {"type": "response.done", "response": {"status": "completed"}}):
            await sock.send_str(json.dumps(msg))
        for _ in range(3):
            got["sent"].append(json.loads((await sock.receive()).data))
        await sock.close()

    async def go():
        runner, url = await serve(server)
        monkeypatch.setattr(openai, "URL", url)
        events = Events()
        link = openai.OpenAILink("sk-1", "", "", "be brief", events)
        await link.connect()
        reader = asyncio.create_task(link.run())
        await asyncio.sleep(0.2)
        await link.respond("[say] hi")
        await link.truncate(1500)
        await asyncio.wait_for(reader, 5)
        await link.close()
        await runner.cleanup()
        return events

    events = asyncio.run(go())
    s = got["session"]["session"]
    assert got["auth"] == "Bearer sk-1" and got["model"] == "gpt-realtime-2.1" and got["session"]["type"] == "session.update"
    assert s["instructions"] == "be brief" and s["audio"]["output"]["voice"] == "marin"
    assert s["audio"]["input"]["turn_detection"] is None and "transcription" not in s["audio"]["input"]   # never answers by itself
    assert [e[0] for e in events.log] == ["ready", "response_start", "audio", "transcript", "response_end"]
    kinds = [m["type"] for m in got["sent"]]
    assert kinds == ["conversation.item.create", "response.create", "conversation.item.truncate"]
    assert got["sent"][0]["item"]["role"] == "system"
    assert got["sent"][2] == {"type": "conversation.item.truncate", "item_id": "i1", "content_index": 0, "audio_end_ms": 1500}


def test_openai_without_credit_is_fatal(monkeypatch):
    async def server(sock, request):
        await sock.receive()
        await sock.send_str(json.dumps({"type": "error", "error": {"type": "insufficient_quota", "code": "credit_balance_exhausted",
                                                                    "message": "You have no credits remaining."}}))
        await sock.close()

    async def go():
        runner, url = await serve(server)
        monkeypatch.setattr(openai, "URL", url)
        try:
            await openai.OpenAILink("sk", "", "", "x", Events()).connect()
        finally:
            await runner.cleanup()

    with pytest.raises(LinkError) as info:
        asyncio.run(go())
    assert info.value.fatal and "no credit" in str(info.value)


# --- RealtimeVoice, with a stand-in link and app ----------------------------------------------------------------------
class FakeSpeaker:
    def __init__(self):
        self.audio, self.ended, self.stopped, self.busy, self.caption = [], 0, 0, False, None
        self.player = None

    def write(self, pcm): self.audio.append(pcm)
    def words(self, text, n): return 0
    def playing_index(self): return None
    def end(self): self.ended += 1
    def stop(self): self.stopped += 1
    def heard_ms(self): return 700
    def close(self): pass


class FakeLink:
    label = "Fake"
    output_rate = 24000
    key_envs = ("FAKE_KEY",)

    def __init__(self, *a, **kw):
        self.sent, self.responding, self.resume_handle, self.resumed = [], False, None, False
        self.model = "m"
        self.closed_reason = "test"
        self.ended = asyncio.Event()

    async def connect(self): pass
    async def run(self): await self.ended.wait()
    async def close(self): self.ended.set()
    async def add_context(self, text): self.sent.append(("context", text))
    async def respond(self, text): self.sent.append(("respond", text))
    async def cancel(self): self.sent.append(("cancel",))
    async def truncate(self, ms): self.sent.append(("truncate", ms))


class FakeBackend:
    label = "Claude Code"
    busy = False

    def __init__(self):
        self.sent = []

    def send(self, text): self.sent.append(text)


class FakeApp:
    def __init__(self, tmp_path, monkeypatch, key="k-123456"):
        monkeypatch.setattr(config, "DATA", tmp_path)
        monkeypatch.setattr(credentials, "registry_env", lambda name: None)
        for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        if key:
            monkeypatch.setenv("GEMINI_API_KEY", key)
        self.profile = profiles.resolve("rt")
        self.cfg = self.profile.config
        self.backend_cfg = self.cfg["backend"]
        self.backend = FakeBackend()
        self.activity = ActivityLog()
        self.credentials = credentials.Credentials(self.profile)
        self.events, self.notices, self.changes = [], [], 0
        self.loop = None

    def event(self, topic, payload=None): self.events.append(topic)
    def notify(self, text): self.notices.append(text)
    def changed(self): self.changes += 1
    def broadcast(self, msg): pass
    def realtime_changed(self): self.changes += 1


@pytest.fixture
def rt(tmp_path, monkeypatch):
    def make(**kw):
        app = FakeApp(tmp_path, monkeypatch, **kw)
        monkeypatch.setitem(RealtimeVoice.__init__.__globals__["LINKS"], "gemini", FakeLink)
        voice = RealtimeVoice(app, stream=False)
        voice.speaker = FakeSpeaker()
        app.activity.listeners.append(lambda e: voice.activity(e.to_json()))
        return app, voice
    return make


def test_the_voice_relays_the_agents_reply_when_it_is_quiet(rt):
    app, voice = rt()

    async def go():
        app.loop = asyncio.get_running_loop()
        await voice.start()
        await asyncio.sleep(0.05)
        assert voice.state == "connected" and voice.handles_speech
        link = voice.link
        turn = app.activity.emit("turn", summary="fix it", input="fix it", status="running")
        app.activity.emit("assistant", turn_id=turn.id, output="Fixed: the tests pass.")
        voice.user_at = -100.0
        await asyncio.sleep(1.2)
        said = [s for s in link.sent if s[0] == "respond"]
        assert len(said) == 1 and "the tests pass" in said[0][1]
        assert any(s[0] == "context" and "fix it" in s[1] for s in link.sent)
        # the model speaks: audio to the speaker, then a "voice" line in the activity log
        voice.on_response_start()
        voice.on_audio(b"\x00\x00" * 10)
        voice.on_transcript("All fixed, ")
        voice.on_transcript("the tests pass.")
        voice.on_response_end(False)
        assert voice.speaker.audio and voice.speaker.ended == 1
        (line,) = [e for e in app.activity.recent() if e["kind"] == "voice"]
        assert line["output"] == "All fixed, the tests pass." and line["turn_id"] == turn.id
        reply = next(e for e in app.activity.recent() if e["kind"] == "assistant")
        assert line["extra"]["relays"] == [reply["id"]] and line["extra"]["same"] is False    # reworded: "Fixed:" vs "All fixed,"
        # what the user said (it went to the agent) is a note, nothing to say
        voice.heard("and run the linter", queued=False)
        await asyncio.sleep(1.2)
        assert any(s[0] == "context" and "and run the linter" in s[1] for s in link.sent)
        assert len([s for s in link.sent if s[0] == "respond"]) == 1
        # cutting it off: stop the sound, cancel the reply, tell the model how much was heard
        voice.on_response_start()
        link.responding = True
        voice.cut_off()
        await asyncio.sleep(0)
        assert ("cancel",) in link.sent and ("truncate", 700) in link.sent and voice.speaker.stopped == 1
        voice.on_audio(b"\x00\x00" * 10)                    # what was still on its way is dropped
        assert len(voice.speaker.audio) == 1
        assert not voice.speaking                          # silent at once, though the service is still sending it
        voice.user_at = -100.0
        assert voice._idle(__import__("time").monotonic())   # and the next reply may start over it
        await voice.stop()

    asyncio.run(go())


def test_the_voice_waits_while_the_user_talks(rt):
    app, voice = rt()

    async def go():
        app.loop = asyncio.get_running_loop()
        await voice.start()
        await asyncio.sleep(0.05)
        voice.narrator.permission("Run: npm test", "Can I run a command?")
        for _ in range(4):                                # the microphone hears the user talking
            voice.user_voice()
            await asyncio.sleep(0.15)
        assert not [s for s in voice.link.sent if s[0] == "respond"]
        await asyncio.sleep(1.2)
        assert [s for s in voice.link.sent if s[0] == "respond"]
        await voice.stop()

    asyncio.run(go())


def test_without_a_key_it_gives_up_and_the_classic_voice_speaks(rt):
    app, voice = rt(key=None)

    async def go():
        app.loop = asyncio.get_running_loop()
        await voice.start()
        await asyncio.sleep(0.05)
        assert voice.state == "failed" and not voice.handles_speech and "no API key" in voice.error
        await voice.stop()

    asyncio.run(go())


# --- the App: who speaks, who listens, where the words go ---------------------------------------------------------------
@pytest.fixture
def app(tmp_path, monkeypatch):
    from wireface.app import App

    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(config, "DEFAULT_CWD", tmp_path)
    a = App(profiles.resolve("routing"), shell=False)
    sent = []
    monkeypatch.setattr(a.backend, "send", lambda text: sent.append(text))
    a.sent = sent
    a.loop = None
    return a


def test_with_the_realtime_voice_the_agents_replies_are_not_read_out(app):
    async def go():
        app.loop = asyncio.get_running_loop()
        await app._apply_realtime()
        assert app.realtime is not None and "start" in app.realtime.calls
        app.realtime.state = "connected"
        said = []
        app._speak_now = said.append
        app.speak("Here is the answer.")
        assert said == [] and app.status()["voice_engine"] == "realtime"
        app.realtime.state = "retrying"                    # the service is away: the classic voice stands in
        app.speak("Here is the answer.")
        assert said == ["Here is the answer."]
        await app.set_config({"voice": {"mode": "classic"}})
        assert app.realtime is None

    asyncio.run(go())


def test_spoken_words_go_to_the_agent_as_they_were_said(app):
    async def go():
        app.loop = asyncio.get_running_loop()
        await app._apply_realtime()
        app.realtime.state = "connected"
        stopped = []
        app._stop_voice = lambda: stopped.append(1)
        app.heard("Please fix the failing test in app dot py")
        assert app.sent == ["Please fix the failing test in app dot py"] and stopped == []
        assert "heard" in app.realtime.calls
        halted = []

        async def halt():
            halted.append(1)

        app.halt = halt
        app.heard("Stop.")
        await asyncio.sleep(0)
        assert halted and app.sent == ["Please fix the failing test in app dot py"]

    asyncio.run(go())


def test_permission_questions_go_through_the_realtime_voice(app):
    async def go():
        app.loop = asyncio.get_running_loop()
        await app._apply_realtime()
        app.realtime.state = "connected"
        asked = asyncio.create_task(app.ask_permission("Run: npm test", "Can I run a command?", False))
        await asyncio.sleep(0)
        assert "permission" in app.realtime.calls
        app.heard("Yes, go ahead.")
        assert await asked == (True, False)
        assert app.sent == [] and [e for e in app.activity.recent() if e["kind"] == "voice" and e["name"] == "user"]

    asyncio.run(go())


def test_the_microphone_is_always_the_speech_recognition_on_this_pc(app, monkeypatch):
    started = []
    monkeypatch.setattr(app.hearing, "start", lambda: started.append("on"))
    monkeypatch.setattr(app.hearing, "stop", lambda: started.append("off"))

    async def go():
        app.loop = asyncio.get_running_loop()
        app.cfg["voice"]["listening"] = True
        await app._apply_realtime()
        app.realtime.state = "connected"
        app._route_mic()
        assert started[-1] == "on"                        # the realtime model never hears the user
        app.realtime.deaf = False                         # barge-in: the microphone listens while it speaks
        app.realtime.speaking = True
        assert not app._deaf()
        app.realtime.deaf = True
        assert app._deaf()
        app.realtime.state = "failed"
        app.realtime.error = "There is no API key for Gemini Live: set GEMINI_API_KEY or paste one in Settings"
        app.realtime_changed()
        assert started[-1] == "on"

    notices = []
    app.notify = notices.append
    asyncio.run(go())
    assert any("needs an API key" in n for n in notices)


def test_profile_accepts_the_realtime_settings():
    base = profiles.defaults()
    cfg = profiles.validate({"voice": {"mode": "classic", "realtime": {"provider": "openai", "narration": "chatty",
                                                                        "openai": {"voice": "cedar"}, "barge_in": True}}}, base)
    assert cfg["voice"]["mode"] == "classic" and cfg["voice"]["realtime"]["openai"]["voice"] == "cedar"
    for bad in ({"voice": {"mode": "loud"}}, {"voice": {"realtime": {"provider": "acme"}}},
                {"voice": {"realtime": {"narration": "always"}}}, {"voice": {"realtime": {"gemini": {"api_key_env": "1x"}}}}):
        with pytest.raises(ValueError):
            profiles.validate(bad, base)



def test_realtime_trouble_is_said_plainly():
    from wireface.app import realtime_trouble

    def say(error, state="retrying"):
        return realtime_trouble("Gemini Live", error, state)

    assert "needs an API key" in say("There is no API key for Gemini Live: set GEMINI_API_KEY", "failed")
    assert "didn't accept the API key" in say("Gemini refused the API key: check GEMINI_API_KEY", "failed")
    assert "usage limit" in say("Gemini Live: RESOURCE_EXHAUSTED quota exceeded")
    assert "doesn't offer the chosen model" in say("Gemini doesn't offer that model for live voice: models/x is not found", "failed")
    assert "internet connection" in say("can't reach Gemini Live: [Errno 11001] getaddrinfo failed")
    assert "problems on its side" in say("Gemini Live: Internal error encountered.")
    assert say("something odd") == "Gemini Live can't connect right now."


def test_without_the_fallback_a_realtime_outage_shows_text_and_says_why(app):
    spoken, shown, notices = [], [], []
    app.notify = notices.append
    app.broadcast = lambda msg: shown.append(msg.get("text")) if msg.get("type") == "say" else None

    class Voice:
        busy = False

        def say(self, text):
            spoken.append(text)

    async def go():
        app.loop = asyncio.get_running_loop()
        app.voice = Voice()
        await app._apply_realtime()
        app.realtime.state, app.realtime.error = "retrying", "Gemini Live: Internal error encountered."
        app.cfg["voice"]["realtime"]["fallback"] = True
        app.speak("The tests pass.")
        assert spoken == ["The tests pass."]
        app.cfg["voice"]["realtime"]["fallback"] = False
        app.realtime_changed()
        app.speak("All done.")
        assert spoken == ["The tests pass."] and shown == ["All done."]       # as text, not the classic voice
        assert app.status()["voice_engine"] == "captions"

    asyncio.run(go())
    assert notices and "problems on its side" in notices[-1] and "Replies show as text" in notices[-1]


def test_profile_takes_the_fallback_switch():
    base = profiles.defaults()
    assert base["voice"]["realtime"]["fallback"] is True
    assert profiles.validate({"voice": {"realtime": {"fallback": False}}}, base)["voice"]["realtime"]["fallback"] is False
    with pytest.raises(ValueError):
        profiles.validate({"voice": {"realtime": {"fallback": "sometimes"}}}, base)
