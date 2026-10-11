"""The judge of what the microphone heard (judge.py): what it is told, what its answers make the face do, and the App
acting on them (with the judge stubbed: no network)."""
import asyncio

import pytest

from wireface import config, judge, profile as profiles
from wireface.judge import Action, Judge, JudgeUnavailable, Moment, Verdict, build_state, questions, route


def v(p_for=0.9, kind="task", confidence=0.9, complete=0.9) -> Verdict:
    return Verdict(p_for=p_for, kind=kind, confidence=confidence, p_complete=complete)


# --- what to do --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("verdict, how, do", [
    (v(), {}, "send"),
    (v(p_for=0.1), {}, "drop"),                                         # not said to it
    (v(p_for=0.4), {}, "hold"),                                         # can't tell: held for the user
    (v(p_for=0.4), {"unsure": "send"}, "send"),                         # ... or sent, as chosen
    (v(p_for=0.4), {"standing": "no"}, "drop"),                         # can't tell, and it sounded like the room
    (v(p_for=0.4), {"standing": "strongly"}, "hold"),
    (v(p_for=0.1), {"named": True}, "send"),                            # "Claude, ..." is for it, whatever it thinks
    (v(complete=0.1), {}, "wait"),                                      # cut off: the rest is coming
    (v(complete=0.1), {"can_wait": False}, "send"),
    (v(p_for=0.1, complete=0.1), {}, "drop"),                           # a cut-off bit of someone else's talk
    (v(kind="stop"), {}, "stop"),
    (v(kind="stop", confidence=0.4), {}, "send"),                       # not sure it means stop: the agent can tell
    (v(kind="approve"), {"permission": True}, "approve"),
    (v(kind="decline"), {"permission": True}, "decline"),
    (v(kind="approve"), {}, "send"),                                    # nothing waiting for an answer
    (v(kind="hide_face"), {}, "hide"),
    (v(kind="show_face"), {}, "show"),
    (v(kind="compact"), {}, "compact"),
    (v(kind="new_conversation", confidence=0.8), {}, "send"),           # forgetting needs to be very sure
    (v(kind="new_conversation", confidence=0.95), {}, "new_conversation"),
    (v(kind="general_question"), {}, "send"),                           # (answered aside: a later phase)
])
def test_route(verdict, how, do):
    assert route(verdict, **how).do == do


# --- what it is told -------------------------------------------------------------------------------------------------
def moment(**kw) -> Moment:
    return Moment(name="Claude", **kw)


def test_the_state_describes_the_sound_and_the_moment_in_words():
    m = moment(recent=[("user", "fix the parser"), ("assistant", "Done. Shall I commit it?")], spoke_s=2.0, quiet_s=2.0,
               working_on=None)
    s = build_state("yeah go on", {"level_vs_user_db": -1.0, "stands_out": "strongly", "lift_db": 20.0}, m)
    assert s["just_heard"] == "yeah go on"
    assert s["how_it_sounded"] == {"length": "one sentence", "loudness": "about as loud as the user's usual voice",
                                   "room": judge.STANDING["strongly"]}
    assert s["moment"] == "right after the assistant asked the user a question"
    assert s["assistant"]["is"] == "idle, waiting for the user" and s["assistant"]["face"] == "on the screen"
    assert s["conversation_so_far"][-1] == {"from": "assistant", "text": "Done. Shall I commit it?"}
    assert "permission_question" not in s

    def numbers(x):
        if isinstance(x, dict):
            return any(numbers(y) for y in x.values())
        if isinstance(x, list):
            return any(numbers(y) for y in x)
        return isinstance(x, (int, float)) and not isinstance(x, bool)
    assert not numbers(s)                                           # Jev is weak with numbers: all in words


@pytest.mark.parametrize("kw, said", [
    ({"quiet_s": None}, "after a long quiet spell"),
    ({"quiet_s": 60.0}, "after a pause"),
    ({"quiet_s": 10.0}, "during a conversation"),
    ({"spoke_s": 1.0, "quiet_s": 1.0}, "right after the assistant finished speaking"),
    ({"permission": "Run rm -rf build?"}, "permission question"),
])
def test_the_moment(kw, said):
    assert said in build_state("hm", None, moment(**kw))["moment"]


@pytest.mark.parametrize("vs_user, said", [(9.0, "louder"), (0.0, "about as loud"), (-7.0, "a little quieter"),
                                           (-15.0, "much quieter"), (None, None)])
def test_loudness(vs_user, said):
    sound = build_state("hm", {"level_vs_user_db": vs_user}, moment())["how_it_sounded"]
    assert (said in sound["loudness"]) if said else "loudness" not in sound


def test_yes_and_no_are_only_offered_while_a_question_waits():
    assert "approve" not in questions(moment())["kind"]["criteria"]
    q = questions(moment(permission="Edit main.py?"))
    assert {"approve", "decline"} <= set(q["kind"]["criteria"])
    assert list(q["kind"]["criteria"])[0] == "task"                  # it leans to the first: the safe one first
    assert build_state("yes", None, moment(permission="Edit main.py?"))["permission_question"] == "Edit main.py?"


def test_long_lines_are_cut():
    s = build_state("x", None, moment(recent=[("user", "a" * 500)]))
    assert len(s["conversation_so_far"][0]["text"]) == judge.LINE_LIMIT


# --- asking it ---------------------------------------------------------------------------------------------------------
ANSWERS = {"answers": {"for_assistant": {"type": "noul", "noul": 0.83},
                       "kind": {"type": "choice", "choice": "task", "confidence": 0.7,
                                "probabilities": {"task": 0.8, "stop": 0.2}},
                       "complete": {"type": "noul", "noul": 0.95}},
           "usage": {"input_tokens": 900, "output_tokens": 0, "cost": 0.00004}}


def test_asking_it(monkeypatch):
    sent = []

    async def post(self, body, key):
        sent.append((body, key))
        return ANSWERS

    monkeypatch.setattr(Judge, "_post", post)
    j = Judge(lambda: "sk-test", lambda: "typesafe/jev-1.13")
    verdict = asyncio.run(j.decide("run the tests", {"stands_out": "somewhat"}, moment()))
    assert (verdict.p_for, verdict.kind, verdict.confidence, verdict.p_complete) == (0.83, "task", 0.7, 0.95)
    body, key = sent[0]
    assert key == "sk-test" and body["model"] == "typesafe/jev-1.13" and body["state"]["just_heard"] == "run the tests"
    assert set(body["questions"]) == {"for_assistant", "kind", "complete"}
    assert j.calls == 1 and j.spent_usd == pytest.approx(0.00004)


def test_without_a_key_or_a_sensible_answer_it_is_unavailable(monkeypatch):
    with pytest.raises(JudgeUnavailable, match="no OpenRouter API key"):
        asyncio.run(Judge(lambda: None, lambda: None).decide("x", None, moment()))

    async def post(self, body, key):
        return {"answers": {"kind": {}}}

    monkeypatch.setattr(Judge, "_post", post)
    j = Judge(lambda: "k", lambda: None)
    with pytest.raises(JudgeUnavailable, match="not understood"):
        asyncio.run(j.decide("x", None, moment()))
    assert "not understood" in j.last_error


# --- the face acting on it -----------------------------------------------------------------------------------------------
@pytest.fixture
def app(tmp_path, monkeypatch):
    from wireface.app import App

    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(config, "DEFAULT_CWD", tmp_path)
    a = App(profiles.resolve("judged"), shell=False)
    a.cfg["voice"]["hearing"]["judge"]["on"] = True
    a.sent = []
    monkeypatch.setattr(a.backend, "send", lambda text: a.sent.append(text))
    a.verdicts, a.asked = [], []

    async def decide(text, info, m):
        a.asked.append(text)
        item = a.verdicts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(a.judge, "decide", decide)
    return a


def run(app, steps):
    async def go():
        app.loop = asyncio.get_running_loop()
        for step in steps:
            if isinstance(step, (int, float)):
                await asyncio.sleep(step)
            else:
                app.heard(step, {"stands_out": "strongly"})
                for _ in range(5):
                    await asyncio.sleep(0)
    asyncio.run(go())


def heard_events(app):
    return [e for e in app.activity.recent() if e["kind"] == "heard"]


def test_what_was_said_to_it_goes_to_the_agent_and_the_rest_is_kept_back(app):
    app.verdicts = [v(), v(p_for=0.05), v(p_for=0.45)]
    run(app, ["run the tests", "I told Alex about the gazebo", "maybe later"])
    assert app.sent == ["run the tests"]
    actions = [(e["output"], e["extra"]["action"]) for e in heard_events(app)]
    assert actions == [("run the tests", "send"), ("I told Alex about the gazebo", "drop"), ("maybe later", "hold")]
    held = heard_events(app)[2]
    assert app.send_held(held["id"]) and app.sent == ["run the tests", "maybe later"]
    assert not app.send_held(held["id"])                            # only once
    assert next(e for e in heard_events(app) if e["id"] == held["id"])["extra"]["sent"] is True


def test_when_the_judge_cant_be_asked_the_words_go_to_the_agent(app):
    app.verdicts = [JudgeUnavailable("can't reach OpenRouter"), JudgeUnavailable("can't reach OpenRouter")]
    run(app, ["run the tests", "and the linter"])
    assert app.sent == ["run the tests", "and the linter"]
    notices = [e for e in app.activity.recent() if e["kind"] == "notice" and "Couldn't judge" in e["summary"]]
    assert len(notices) == 1                                        # said once, not every time


def test_a_cut_off_sentence_waits_for_the_rest(app, monkeypatch):
    monkeypatch.setattr(judge, "MERGE_WAIT_S", 0.05)
    app.verdicts = [v(complete=0.05), v()]
    run(app, ["can I get", "the test results for the parser"])
    assert app.asked == ["can I get", "can I get the test results for the parser"]
    assert app.sent == ["can I get the test results for the parser"]


def test_a_cut_off_sentence_with_nothing_after_it_is_taken_as_it_is(app, monkeypatch):
    monkeypatch.setattr(judge, "MERGE_WAIT_S", 0.05)
    app.verdicts = [v(complete=0.05)]
    run(app, ["what about the", 0.15])
    assert app.sent == ["what about the"]


def test_commands_in_its_own_words(app, monkeypatch):
    halted = []

    async def halt():
        halted.append(1)

    monkeypatch.setattr(app, "halt", halt)
    app.verdicts = [v(kind="stop"), v(kind="hide_face"), v(kind="show_face")]
    run(app, ["okay that's enough for now", "could you get off the screen", "I want to see you again"])
    assert halted and app.sent == [] and app.face_visible


def test_a_permission_answer_in_its_own_words(app):
    answers = []
    app.permission = {"id": "p1", "text": "Delete build/?", "can_always": False}
    app.answer_permission = lambda allow, always=False, request_id=None: answers.append(allow) or True
    app.verdicts = [v(kind="decline")]
    run(app, ["nah leave that folder alone"])
    assert answers == [False] and app.sent == []


def test_typed_text_and_the_judge_switched_off_are_not_judged(app):
    app.chat("typed: run the tests")
    app.cfg["voice"]["hearing"]["judge"]["on"] = False
    run(app, ["spoken: run the linter"])
    assert app.asked == [] and app.sent == ["typed: run the tests", "spoken: run the linter"]


def test_stop_and_yes_stay_instant(app, monkeypatch):
    halted = []

    async def halt():
        halted.append(1)

    monkeypatch.setattr(app, "halt", halt)
    run(app, ["stop"])
    assert halted and app.asked == []                               # never waits for the network


def test_the_settings_page_sees_the_judge_but_never_the_key(app, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-123456")
    app.credentials.forget_cache()
    view = app.config_view()
    assert view["judge"]["available"] and view["judge"]["source"] == "env:OPENROUTER_API_KEY"
    assert "sk-or-test-123456" not in str(view)
    assert "sk-or-test-123456" in app.credentials.secret_values()   # kept out of the activity log


def test_the_advanced_settings_validate():
    base = profiles.defaults()
    assert base["voice"]["hearing"]["surprise"] is False and base["voice"]["hearing"]["judge"]["on"] is False
    out = profiles.validate({"voice": {"hearing": {"surprise": True, "judge": {"on": True, "unsure": "send"}}}}, base)
    assert out["voice"]["hearing"]["surprise"] and out["voice"]["hearing"]["judge"]["unsure"] == "send"
    with pytest.raises(ValueError, match="unsure"):
        profiles.validate({"voice": {"hearing": {"judge": {"unsure": "maybe"}}}}, base)
