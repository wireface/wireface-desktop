"""A face's side of the room: when the agent's words are spoken, and what is reported back."""
import asyncio
from types import SimpleNamespace

import pytest

from wireface.activity import ActivityLog
from wireface.room.face import RoomLink


class FakeClient:
    connected = True

    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)

    def types(self):
        return [m["type"] for m in self.sent]


class FakeBackend:
    id = "claude"

    def __init__(self):
        self.prompts = []

    def send(self, text):
        self.prompts.append(text)


class FakeApp:
    def __init__(self):
        self.cfg = {"room": {"name": "team", "aliases": [], "lead": "first"}, "name": "Claude", "voice": {"on": True, "listening": False}}
        self.backend = FakeBackend()
        self.activity = ActivityLog()
        self.voice = None
        self.spoken, self.halts, self.answers = [], [], []

    def _speak_now(self, text): self.spoken.append(text)
    def changed(self): pass
    def answer_permission(self, allow): self.answers.append(allow)

    async def halt(self, from_room=False):
        self.halts.append(from_room)


def link():
    app = FakeApp()
    l = RoomLink(app, "team")
    l.client = FakeClient()
    l.me = "Claude"
    return l, app


def ask(l, round_, floor, kind="ask", prompt="the prompt"):
    l.on_message({"type": kind, "round": round_, "floor": floor, "prompt": prompt})


def whole_turn(l, *pieces, source="user"):
    l.turn_started(source)
    for p in pieces:
        l.speak(p)
    l.turn_done(source)


def test_an_addressed_agent_speaks_at_once_and_frees_the_floor_when_its_voice_is_quiet():
    l, app = link()
    ask(l, 1, floor=True)
    assert app.backend.prompts == ["the prompt"]
    l.turn_started("user")
    assert l.speak("Hello there.") is True and app.spoken == ["Hello there."]
    l.turn_done("user")
    assert l.client.sent == [{"type": "reply", "round": 1, "kind": "ask", "text": "Hello there.", "passed": False}]
    l.poll()
    assert l.client.types() == ["reply", "speech_done"]
    l.poll()
    assert l.client.types().count("speech_done") == 1                       # once


def test_a_racing_agent_holds_what_it_says_and_claims_the_floor_once():
    l, app = link()
    ask(l, 1, floor=False)
    l.turn_started("user")
    l.speak("First sentence.")
    l.speak("Second sentence.")
    assert app.spoken == [] and l.client.sent == [{"type": "claim", "round": 1}]      # one claim, nothing spoken
    l.on_message({"type": "floor", "round": 1})
    assert app.spoken == ["First sentence.", "Second sentence."]                      # in order, all of it
    l.speak("Third.")
    assert app.spoken[-1] == "Third."                                                 # now live
    l.turn_done("user")
    l.poll()
    assert l.client.types() == ["claim", "reply", "speech_done"]


def test_a_racing_agent_that_lost_never_speaks_but_still_reports_its_answer():
    l, app = link()
    ask(l, 1, floor=False)
    whole_turn(l, "My answer, too late.")
    assert app.spoken == []
    assert l.client.sent[-1] == {"type": "reply", "round": 1, "kind": "ask", "text": "My answer, too late.", "passed": False}
    l.poll()
    assert "speech_done" not in l.client.types()                            # it never had the floor
    assert l.waiting is True                                                # but it is holding an answer


def test_the_floor_can_arrive_after_the_agent_finished():
    l, app = link()
    ask(l, 1, floor=False)
    whole_turn(l, "Late but first.")
    assert app.spoken == []
    l.on_message({"type": "floor", "round": 1})
    assert app.spoken == ["Late but first."]
    l.poll()
    assert l.client.types()[-1] == "speech_done" and l.waiting is False


def test_a_contribution_that_is_just_pass_is_never_spoken_and_is_reported_as_a_pass():
    l, app = link()
    ask(l, 1, floor=True, kind="contribute")
    l.turn_started("user")
    l.speak("PA")
    l.speak("SS")
    assert app.spoken == []                                                 # held while it might be PASS
    l.turn_done("user")
    assert l.client.sent == [{"type": "reply", "round": 1, "kind": "contribute", "text": "PA SS", "passed": True}]
    assert app.spoken == []
    l.poll()
    assert "speech_done" not in l.client.types()


def test_pass_in_one_piece():
    l, app = link()
    ask(l, 1, floor=True, kind="contribute")
    whole_turn(l, "PASS")
    assert app.spoken == [] and l.client.sent == [{"type": "reply", "round": 1, "kind": "contribute", "text": "PASS", "passed": True}]
    l.poll()
    assert "speech_done" not in l.client.types()                            # nothing was said: nothing to wait for


def test_a_real_contribution_is_spoken_straight_away():
    l, app = link()
    ask(l, 1, floor=True, kind="contribute")
    l.turn_started("user")
    l.speak("Check the cache.")
    assert app.spoken == ["Check the cache."]
    l.speak("Also the index.")
    assert app.spoken == ["Check the cache.", "Also the index."]


def test_a_contribution_that_starts_like_pass_but_is_not_is_released():
    l, app = link()
    ask(l, 1, floor=True, kind="contribute")
    l.turn_started("user")
    l.speak("Pa")
    assert app.spoken == []
    l.speak("rtitioning will help here.")
    assert app.spoken == ["Pa", "rtitioning will help here."]


def test_the_floor_is_kept_until_the_voice_is_quiet():
    l, app = link()
    app.voice = SimpleNamespace(busy=True)
    ask(l, 1, floor=True)
    whole_turn(l, "A long answer.")
    l.poll()
    assert "speech_done" not in l.client.types()
    app.voice.busy = False
    l.poll()
    assert l.client.types()[-1] == "speech_done"


def test_stop_silences_the_turn_that_is_still_finishing_and_drops_queued_prompts():
    async def go():
        l, app = link()
        ask(l, 1, floor=True)
        l.turn_started("user")
        ask(l, 2, floor=False)                                              # queued behind it
        l.on_message({"type": "stop"})
        await asyncio.sleep(0.01)
        assert not l.queue and app.halts == [True]                         # the agent is halted; the room is not told again
        assert l.speak("words from the interrupted turn") is True and app.spoken == []
        l.turn_done("user")
        assert l.client.types() == []                                       # no reply for a cancelled turn

    asyncio.run(go())


def test_a_new_prompt_after_a_stop_gets_its_own_turn():
    async def go():
        l, app = link()
        ask(l, 1, floor=True)
        l.turn_started("user")
        l.on_message({"type": "stop"})
        ask(l, 2, floor=True)
        l.turn_done("user")                                                 # the interrupted turn ends
        l.turn_started("user")                                              # then the new one starts
        l.speak("Answer to the new question.")
        l.turn_done("user")
        assert l.client.sent[-1]["round"] == 2 and app.spoken == ["Answer to the new question."]

    asyncio.run(go())


def test_prompts_are_matched_to_turns_in_order():
    l, app = link()
    ask(l, 1, floor=False)
    ask(l, 2, floor=True, kind="contribute")
    l.turn_started("user")
    assert l.current.round == 1
    l.turn_done("user")
    l.turn_started("user")
    assert l.current.round == 2


def test_speech_nobody_asked_for_waits_for_its_turn():
    l, app = link()
    l.turn_started("background")
    l.speak("The tests finished.")
    assert app.spoken == [] and l.client.sent == [{"type": "floor_request"}]
    l.on_message({"type": "floor", "round": None})
    assert app.spoken == ["The tests finished."]
    l.turn_done("background")
    assert "reply" not in l.client.types()                                  # not part of any question
    l.poll()
    assert l.client.types()[-1] == "speech_done"


def test_outside_a_job_the_caller_speaks_it_normally():
    l, app = link()
    assert l.speak("anything") is False


def test_permission_answers_and_notices():
    l, app = link()
    l.on_message({"type": "answer", "allow": True})
    l.on_message({"type": "notice", "text": "Codex leads"})
    assert app.answers == [True]
    assert [e["summary"] for e in app.activity.recent()] == ["Room: Codex leads"]


def test_the_view_for_the_page():
    l, app = link()
    assert l.view() == {"name": "team", "connected": True, "me": "Claude", "listening": False, "phase": "idle",
                        "lead": None, "floor": None, "members": [], "waiting": False}
    l.on_message({"type": "room", "listening": True, "phase": "lead", "lead": "Codex", "floor": "Codex", "members": ["Claude", "Codex"]})
    assert l.view()["lead"] == "Codex" and l.view()["listening"] is True
    assert RoomLink(app, None).view() is None


def test_leaving_the_connection_drops_everything_held():
    l, app = link()
    ask(l, 1, floor=False)
    l.turn_started("user")
    l.speak("held")
    l._on_state(False)
    assert l.current.cancelled and l.status == {}
