import pytest

from wireface.room import envelope, names

MEMBERS = {
    "c": names.aliases_for("Claude", "claude"),
    "x": names.aliases_for("Codex", "codex"),
    "o": names.aliases_for("OpenCode", "opencode", ["oc"]),
}


def test_aliases_cover_the_name_the_kind_and_extras():
    assert "claude" in MEMBERS["c"] and "claud" in MEMBERS["c"]
    assert "code x" in MEMBERS["x"] and "open code" in MEMBERS["o"] and "oc" in MEMBERS["o"]
    assert names.aliases_for("Ada") == ["ada"]
    assert names.aliases_for("Research Bot", "", ["rb"]) == ["research bot", "rb"]       # longest first


@pytest.mark.parametrize("said, member, rest", [
    ("Codex, what do you think?", "x", "what do you think"),
    ("hey claude can you check this", "c", "can you check this"),
    ("Okay open code run the tests", "o", "run the tests"),
    ("OpenCode: status", "o", "status"),
    ("Hey, OK Codex do it", "x", "do it"),
    ("Codex", "x", ""),
])
def test_a_name_at_the_start_addresses_that_agent(said, member, rest):
    d = names.parse_directed(said, MEMBERS)
    assert (d.member, d.everyone, d.text) == (member, False, rest)


def test_everyone_and_plain_speech():
    d = names.parse_directed("Everyone, status please", MEMBERS)
    assert d.everyone and d.member is None and d.text == "status please"
    d = names.parse_directed("What does Claude think about this?", MEMBERS)        # a name in the middle is just a word
    assert (d.member, d.everyone) == (None, False) and d.text == "what does claude think about this"
    d = names.parse_directed("Can you open code review for me", MEMBERS)
    assert d.member is None


def test_finding_names_in_order():
    assert names.find_names("claude and codex and claude again", MEMBERS) == [(0, "c"), (2, "x"), (4, "c")]
    assert names.count_names("claude and codex and claude again", MEMBERS) == {"c": 2, "x": 1}
    assert names.find_names("nothing here", MEMBERS) == []


def test_echo_ratio():
    spoken = "The migration looks safe but check the cache invalidation before you ship it."
    assert names.echo_ratio(spoken, spoken) == pytest.approx(1.0, abs=0.01)
    assert names.echo_ratio("migration looks safe but check the cache", spoken) > 0.9       # a part of it
    assert names.echo_ratio("looks safe check cache invalidation ship", spoken) > 0.75        # a garbled echo
    assert names.echo_ratio("what is the weather in paris tomorrow", spoken) < 0.3
    assert names.echo_ratio("", spoken) == 0.0 and names.echo_ratio("something", "") == 0.0


SPOKEN = "The migration looks safe but check the cache invalidation before you ship it."


def heard(text, spoken=SPOKEN):
    return names.classify(text, spoken, MEMBERS)


def test_the_agents_own_voice_coming_back_through_the_microphone_is_ignored():
    assert heard("the migration looks safe but check the cache").kind == "echo"
    assert heard("looks safe check the cache invalidation before you ship").kind == "echo"


def test_the_users_name_for_an_agent_interrupts_even_over_the_echo():
    h = heard("the migration looks safe but check the cache codex")
    assert (h.kind, h.member) == ("interrupt", "x")
    h = heard("Codex")
    assert (h.kind, h.member, h.rest) == ("interrupt", "x", "")


def test_what_the_user_says_after_the_name_is_kept_but_leftover_echo_is_not():
    h = heard("codex what about the database indexes")
    assert (h.kind, h.member, h.rest) == ("interrupt", "x", "what about the database indexes")
    h = heard("codex migration looks safe but check the cache")
    assert h.kind == "interrupt" and h.rest == ""


def test_an_agent_saying_another_agents_name_does_not_interrupt_that_agent():
    spoken = "Claude thinks the migration is safe, but Claude has not seen the cache."
    assert names.classify("claude thinks the migration is safe but claude has not seen the cache", spoken, MEMBERS).kind == "echo"
    # but the user adding one more mention does
    h = names.classify("claude thinks the migration is safe but claude has not seen the cache claude", spoken, MEMBERS)
    assert (h.kind, h.member) == ("interrupt", "c")


def test_a_name_with_a_stop_word_is_not_a_question():
    for said in ("codex stop", "Codex, please stop now", "codex wait a second"):
        h = heard(said)
        assert (h.kind, h.member, h.rest) == ("interrupt", "x", ""), said


def test_stop_words_interrupt_without_a_name():
    for said in ("stop", "wait a second", "hold on", "okay be quiet"):
        h = heard(said)
        assert (h.kind, h.member, h.stop) == ("interrupt", None, True), said
    spoken = "You should not stop the service, wait for the queue to drain."
    assert names.classify("you should not stop the service wait for the queue", spoken, MEMBERS).kind == "echo"


def test_unrelated_talking_over_an_agent_is_ignored():
    assert heard("what time is it in tokyo").kind == "ignore"
    assert heard("").kind == "ignore" and heard("  ...  ").kind == "ignore"


# --- the prompts ---------------------------------------------------------------------------------------------------
def test_pass_detection():
    for text in ("PASS", "pass", "Pass.", " (PASS) ", "", "  "):
        assert envelope.is_pass(text), text
    for text in ("I pass on that", "Passing thought: the cache", "Looks good"):
        assert not envelope.is_pass(text), text


def test_a_reply_that_might_still_become_pass_is_held_back():
    for partial in ("", "P", "PA", "PAS", "PASS", "pass."):
        assert envelope.might_become_pass(partial), partial
    for partial in ("Pa" + "tience", "The", "Looks good", "PASSING"):
        assert not envelope.might_become_pass(partial), partial


LINES = [("User", "is it safe?"), ("Claude", "Mostly.")]


def test_prompts_carry_who_said_what_and_what_to_do():
    ask = envelope.ask_prompt("Codex", ["Claude", "OpenCode"], LINES, first_to_answer=True)
    assert "You are Codex" in ask and "Claude, OpenCode" in ask and "[User]: is it safe?" in ask
    assert "[Claude]: Mostly." in ask and "answers first" in ask
    direct = envelope.ask_prompt("Codex", [], LINES, first_to_answer=False)
    assert "directly" in direct and "no one else" in direct
    contribute = envelope.contribute_prompt("Codex", ["Claude"], LINES)
    assert "exactly PASS" in contribute and "[Claude]: Mostly." in contribute and "Do not make changes" in contribute
    final = envelope.final_prompt("Claude", ["Codex"], LINES)
    assert "last word" in final and "do not ask the others" in final
    for prompt in (ask, contribute, final):
        assert "aloud" in prompt and "no markdown" in prompt
