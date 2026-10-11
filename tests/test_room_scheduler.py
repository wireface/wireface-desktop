import pytest

from wireface.room.scheduler import (Ask, Contribute, Final, GrantFloor, Halt, Limits, Notice, Scheduler)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def room(*names, lead="first"):
    clock = Clock()
    s = Scheduler(lead=lead, clock=clock)
    ids = {}
    for n in names:
        ids[n] = n.lower()
        s.join(ids[n], n)
    return s, clock


def kinds(acts):
    """Actions without the notices, as (type, member) pairs."""
    return [(type(a).__name__, getattr(a, "member", None)) for a in acts if not isinstance(a, Notice)]


def notices(acts):
    return [a.text for a in acts if isinstance(a, Notice)]


def speak(s, member, text):
    """A member's whole turn: it has something to say, says it, finishes, and its voice goes quiet."""
    acts = s.claim(member)
    acts += s.reply(member, text)
    acts += s.speech_done(member)
    return acts


def test_everyone_races_and_the_first_to_speak_leads_then_the_others_add_and_the_lead_concludes():
    s, _ = room("Claude", "Codex", "OpenCode")
    acts = s.user_says("is this safe?")
    assert kinds(acts) == [("Ask", "claude"), ("Ask", "codex"), ("Ask", "opencode")]
    assert all(a.floor is False for a in acts if isinstance(a, Ask))

    assert kinds(s.claim("codex")) == [("GrantFloor", "codex")] and s.floor == "codex"
    assert s.claim("claude") == [] and s.floor == "codex"                 # too late: it has to hold its answer
    assert s.reply("claude", "my own draft") == []                         # held, not spoken
    assert s.reply("codex", "Mostly yes.") == []                          # its audio is still playing
    assert kinds(s.speech_done("codex")) == [("Contribute", "claude")]     # claude finished first: it goes first
    assert s.floor == "claude"

    assert s.reply("claude", "One risk: the cache.") == []
    assert kinds(s.speech_done("claude")) == [("Contribute", "opencode")]
    assert kinds(s.reply("opencode", "PASS", passed=True)) == [("Final", "codex")]   # nothing to say: no waiting
    assert s.floor == "codex"
    assert s.reply("codex", "So, safe apart from the cache.") == []
    assert s.speech_done("codex") == [] and s.round is None and s.floor is None

    lines = [f"{who}: {text}" for who, text in s.transcript.lines]
    assert lines == ["User: is this safe?", "Codex: Mostly yes.", "Claude: One risk: the cache.",
                     "Codex: So, safe apart from the cache."]


def test_a_passing_agent_is_not_in_the_conversation():
    s, _ = room("Claude", "Codex")
    s.user_says("q")
    speak(s, "claude", "Answer.")
    s.reply("codex", "", passed=True)
    assert all(who != "Codex" for who, _ in s.transcript.lines)


def test_an_addressed_agent_leads_alone_and_the_others_comment_in_room_order():
    s, _ = room("Claude", "Codex", "OpenCode")
    acts = s.user_says("Codex, review this", directed="codex")
    assert kinds(acts) == [("Ask", "codex")] and acts[0].floor is True and s.floor == "codex"
    assert s.reply("codex", "Looks fine.") == [] and kinds(s.speech_done("codex")) == [("Contribute", "claude")]
    s.reply("claude", "Agreed.")
    assert kinds(s.speech_done("claude")) == [("Contribute", "opencode")]
    s.reply("opencode", "Also agreed.")
    assert kinds(s.speech_done("opencode")) == [("Final", "codex")]


def test_when_everyone_passes_the_lead_has_the_last_word_already_and_there_is_no_final():
    s, _ = room("Claude", "Codex")
    s.user_says("q")
    speak(s, "claude", "Done.")
    assert kinds(s.reply("codex", "PASS", passed=True)) == []
    assert s.round is None


def test_a_round_nobody_answers_ends():
    s, _ = room("Claude", "Codex")
    s.user_says("q")
    s.reply("claude", "", passed=True)
    assert "nobody had anything to say" in notices(s.reply("codex", ""))
    assert s.round is None


def test_replying_without_claiming_first_still_takes_the_floor():
    s, _ = room("Claude", "Codex")
    s.user_says("q")
    acts = s.reply("codex", "Quick answer.")
    assert kinds(acts) == [("GrantFloor", "codex")] and s.round.lead == "codex"


def test_a_contributor_that_takes_too_long_is_skipped_and_stopped():
    s, clock = room("Claude", "Codex", "OpenCode")
    s.user_says("q")
    speak(s, "claude", "Answer.")
    assert s.round.current == "codex"
    clock.now += 91
    acts = s.tick()
    assert kinds(acts) == [("Halt", "codex"), ("Contribute", "opencode")]
    assert notices(acts)[0] == "Codex took too long"
    assert s.reply("codex", "late!") == []                                  # a reply that arrives after is ignored
    assert "Codex" not in [w for w, _ in s.transcript.lines]


def test_the_lead_cannot_run_away_with_the_conclusion():
    s, clock = room("Claude", "Codex")
    s.user_says("q")
    speak(s, "claude", "A.")
    speak(s, "codex", "B.")
    assert s.round.phase == "final"
    clock.now += 91
    acts = s.tick()
    assert kinds(acts) == [("Halt", "claude")] and s.round is None


def test_a_round_that_goes_on_too_long_is_ended():
    s, clock = room("Claude", "Codex")
    s.user_says("q")
    clock.now += 601
    assert kinds(s.tick()) == [("Halt", None)] and s.round is None


def test_the_user_speaking_again_stops_everything_and_starts_over():
    s, _ = room("Claude", "Codex")
    s.user_says("first")
    s.claim("claude")
    old = s.round.id
    acts = s.user_says("second")
    assert kinds(acts)[0] == ("Halt", None) and s.round.id == old + 1 and s.floor is None
    assert s.reply("claude", "answer to the first", round=old) == []       # stale: it answers the earlier question
    assert s.claim("codex", round=old) == [] and s.round.lead is None
    assert [t for _, t in s.transcript.lines if t == "answer to the first"] == []


def test_interrupt_clears_the_round_and_the_floor():
    s, _ = room("Claude", "Codex")
    s.user_says("q")
    s.claim("codex")
    assert kinds(s.interrupt()) == [("Halt", None)] and s.round is None and s.floor is None


def test_a_member_leaving_mid_round():
    s, _ = room("Claude", "Codex", "OpenCode")
    s.user_says("q")
    speak(s, "claude", "A.")
    assert s.round.current == "codex"
    assert kinds(s.leave("codex")) == [("Contribute", "opencode")]          # skipped
    s2, _ = room("Claude", "Codex")
    s2.user_says("q")
    s2.claim("claude")
    s2.leave("claude")
    assert s2.round is None                                                 # the lead left: the round ends


def test_one_agent_alone_just_answers():
    s, _ = room("Claude")
    acts = s.user_says("q")
    assert kinds(acts) == [("Ask", "claude")] and acts[0].floor
    s.reply("claude", "Hi.")
    assert s.speech_done("claude") == [] and s.round is None


def test_a_pinned_lead_answers_alone_unless_the_user_says_everyone():
    s, _ = room("Claude", "Codex", lead="Codex")
    assert kinds(s.user_says("q")) == [("Ask", "codex")]
    s2, _ = room("Claude", "Codex", lead="Codex")
    assert [k for k, _ in kinds(s2.user_says("q", everyone=True))] == ["Ask", "Ask"]


def test_a_member_that_sat_a_round_out_catches_up_on_its_next_prompt():
    s, _ = room("Claude", "Codex")
    s.user_says("Claude, question one", directed="claude")
    s.transcript.take("claude")                                              # (the room hands it the prompt)
    speak(s, "claude", "One.")
    s.reply("codex", "PASS", passed=True)
    assert s.round is None
    seen = s.transcript.take("codex")
    assert [t for _, t in seen] == ["Claude, question one", "One."]
    assert s.transcript.take("codex") == []                                  # now seen
    assert [t for _, t in s.transcript.peek("claude")] == []                 # it has seen the question and its own answer


def test_a_new_member_is_not_given_the_whole_history():
    s, _ = room("Claude")
    for i in range(20):
        s.transcript.add("User", f"line {i}")
    s.join("late", "Late")
    assert len(s.transcript.take("late")) == Limits().history


def test_there_is_exactly_one_pass_over_the_members_so_no_loops():
    """With four agents: one Ask each, one Contribute for each of the three others, one Final. Whatever they say."""
    s, _ = room("A", "B", "C", "D")
    acts = list(s.user_says("q"))
    acts += s.claim("a")
    acts += s.reply("a", "lead says something")
    acts += s.speech_done("a")
    for member in ("b", "c", "d"):
        acts += s.reply(member, "I disagree, A should reconsider and B should answer me")
        acts += s.speech_done(member)
    acts += s.reply("a", "conclusion")
    acts += s.speech_done("a")
    count = lambda t: sum(isinstance(x, t) for x in acts)
    assert (count(Ask), count(Contribute), count(Final)) == (4, 3, 1)
    assert s.round is None
    # nothing a member says can start anything: replies after the round do nothing
    assert s.reply("b", "and another thing") == [] and s.claim("c") == [] and s.speech_done("d") == []


def test_only_one_member_holds_the_floor_at_any_moment():
    s, _ = room("A", "B", "C")
    s.user_says("q")
    holders = []
    for act in s.claim("b") + s.claim("a") + s.claim("c"):
        holders.append(s.floor)
    s.reply("b", "x")
    for act in s.speech_done("b"):
        holders.append(s.floor)
    assert set(holders) <= {"b", "a"} and s.floor == "a"


def test_speech_outside_a_round_takes_turns():
    s, _ = room("Claude", "Codex")
    assert kinds(s.floor_request("claude")) == [("GrantFloor", "claude")]
    assert s.floor_request("codex") == []                                    # waits
    assert kinds(s.speech_done("claude")) == [("GrantFloor", "codex")]
    assert s.speech_done("codex") == [] and s.floor is None


def test_speech_that_wants_the_floor_during_a_round_waits_until_it_ends():
    s, _ = room("Claude", "Codex")
    s.user_says("q")
    speak(s, "claude", "A.")
    assert s.floor_request("codex") == []                                    # codex is about to contribute anyway
    s.reply("codex", "B.")
    acts = s.speech_done("codex")
    assert ("Final", "claude") in kinds(acts)
    speak_acts = s.reply("claude", "C.") + s.speech_done("claude")
    assert kinds(speak_acts) == [("GrantFloor", "codex")]


def test_with_no_agents_there_is_nobody_to_ask():
    s, _ = room()
    assert notices(s.user_says("hello")) == ["no agents are in the room"]


def test_status_for_the_panel():
    s, _ = room("Claude", "Codex")
    assert s.status() == {"phase": "idle", "lead": None, "floor": None, "members": ["Claude", "Codex"]}
    s.user_says("q")
    s.claim("codex")
    assert s.status()["lead"] == "Codex" and s.status()["floor"] == "Codex" and s.status()["phase"] == "lead"


def test_a_late_answer_to_the_question_is_not_mistaken_for_a_comment():
    """The agent whose turn it is to comment may still be finishing its first answer; that must not count."""
    s, _ = room("Claude", "Codex")
    s.user_says("q")
    speak(s, "claude", "Lead answer.")
    assert s.round.phase == "contrib" and s.round.current == "codex"
    assert s.reply("codex", "my slow first draft", round=s.round.id, kind="ask") == []     # ignored as a comment
    assert s.round.current == "codex" and s.round.contributions == []
    assert [t for _, t in s.transcript.lines] == ["q", "Lead answer."]
    assert s.reply("codex", "the actual comment", round=s.round.id, kind="contribute") == []
    acts = s.speech_done("codex")
    assert ("Final", "claude") in kinds(acts)
    assert [t for _, t in s.transcript.lines][-1] == "the actual comment"


def test_a_comment_reply_in_the_wrong_phase_is_ignored():
    s, _ = room("Claude", "Codex")
    s.user_says("q")
    assert s.reply("codex", "early comment", kind="contribute") == []
    assert s.reply("codex", "stray conclusion", kind="final") == []
    assert s.round.phase == "lead" and s.round.lead is None
