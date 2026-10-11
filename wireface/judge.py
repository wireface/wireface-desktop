"""Judging what the microphone heard before the agent gets it ("Judge what it hears", Settings > Voice > Advanced).

Speech recognition writes down whatever is said near the microphone: talk to someone else, the TV, "yeah", "mm", a
garbled word from a clatter. Without a judge all of it goes to the agent. With one, a decision model (Jev, by
TypeSafe, through OpenRouter's Decisions API) reads the words and a short description of the moment, and answers three
typed questions in one call (~0.1-0.5 s, a tiny fraction of a cent):

    for_assistant   was it said to the assistant? (a probability)
    kind            what is it: a task, a follow-up, "stop", yes/no to a permission question, "go away", ...
    complete        is it a whole thought, or was it cut off mid-sentence (the rest is probably coming)?

``route`` turns the answers into what the face does (send it, drop it, hold it for the user to send, wait for the
rest, or carry out a command); it is pure, so the rules are tested without a network. Jev reads text only: the sound
of it (how loud against the user's usual voice, how much it stood out from the room) is described in words, never as
numbers, which it is weak at. When the judge can't be asked (no key, no network, too slow) the face does what it did
before: the words go to the agent.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger(__name__)

URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "~typesafe/jev-latest"
TIMEOUT_S = 2.0              # then the words go to the agent unjudged
FOR_ME = 0.6                 # for_assistant at least this: it was for the agent
NOT_FOR_ME = 0.25            # below this: it wasn't (dropped)
NOT_FOR_ME_BACKGROUND = 0.5  # ... or below this when it also sounded like the room's background
COMMAND = 0.6                # how sure of the kind before a command is carried out (else the words are sent)
NEW_CONVERSATION = 0.9       # forgetting the conversation needs more
CUT_OFF = 0.3                # complete below this: wait for the rest
MERGE_WAIT_S = 2.0           # how long to wait for the rest of a cut-off sentence
RECENT = 6                   # lines of the conversation the judge sees
LINE_LIMIT = 200             # characters of each

# The kinds, as the judge is told them. Jev leans towards the first option: the first is the safe one (the words go to
# the agent). "approve" and "decline" are only offered while a permission question is waiting.
KINDS: dict[str, str | dict] = {
    "task": "Asks the assistant to do, find out, explain or make something.",
    "follow_up": "Continues the conversation with the assistant: answers its question, adds a detail, or reacts to "
                 "what it just said.",
    "status_question": "Asks how the assistant's current work is going or what it is doing right now.",
    "correction": {"what": "Changes or redirects the work the assistant is doing right now.",
                   "examples": ["no, use the other file", "actually make it blue", "not that folder"]},
    "stop": {"what": "Tells the assistant to stop talking or stop working, and asks for nothing else.",
             "examples": ["stop", "hang on", "that's enough", "never mind"],
             "not_for": "a sentence that also asks for something else (that is a correction or a task)"},
    "approve": "Says yes to the permission question the assistant is waiting on.",
    "decline": "Says no to the permission question the assistant is waiting on.",
    "hide_face": "Tells the assistant's face to go away or hide from the screen.",
    "show_face": "Asks the assistant's face to come back or show itself on the screen.",
    "compact": "Asks the assistant to compact or summarise its conversation to free up its memory.",
    "new_conversation": {"what": "Asks the assistant to start a new conversation and forget this one.",
                         "not_for": "starting a new task or file (that is a task)"},
    "general_question": "A quick general question that has nothing to do with the assistant's work (the weather, a "
                        "fact, a word's meaning, the time).",
    "small_talk": "A greeting, thanks, or a remark to the assistant with no request in it.",
}
PERMISSION_KINDS = ("approve", "decline")


class JudgeUnavailable(Exception):
    """The judge can't be asked right now (the reason is in the message): fall back to sending the words."""


# --- what the judge is told --------------------------------------------------------------------------------------
@dataclass
class Moment:
    """What was going on when the words were heard (the App fills it in)."""
    name: str                                        # the assistant's name
    aliases: list[str] = field(default_factory=list)
    recent: list[tuple[str, str]] = field(default_factory=list)    # ("user" | "assistant", text), oldest first
    working_on: str | None = None                    # what the agent is busy with; None when idle
    permission: str | None = None                    # the question it is waiting on
    face_visible: bool = True
    spoke_s: float | None = None                     # seconds since the face last spoke (None: not this session)
    quiet_s: float | None = None                     # seconds since the last line of the conversation


def _loudness(vs_user: float | None) -> str | None:
    if vs_user is None:
        return None
    if vs_user > 6:
        return "louder than the user's usual voice"
    if vs_user >= -4:
        return "about as loud as the user's usual voice"
    if vs_user >= -10:
        return "a little quieter than the user's usual voice"
    return "much quieter than the user's usual voice (further away)"


STANDING = {"strongly": "stands out strongly from the room's usual background sound",
            "somewhat": "stands out a little from the room's background sound",
            "no": "sounds like the room's ongoing background sound (a TV, radio or other people)"}


def _length(text: str) -> str:
    n = len(text.split())
    return "a word or two" if n <= 2 else "one sentence" if n <= 15 else "long"


def _moment(m: Moment) -> str:
    asked = bool(m.recent) and m.recent[-1][0] == "assistant" and m.recent[-1][1].rstrip().endswith("?")
    if m.permission:
        return "while the assistant waits for the user's answer to a permission question"
    if m.spoke_s is not None and m.spoke_s < 6:
        return ("right after the assistant asked the user a question" if asked
                else "right after the assistant finished speaking")
    if m.quiet_s is None or m.quiet_s > 120:
        return "after a long quiet spell (no conversation with the assistant for over two minutes)"
    if m.quiet_s > 30:
        return "after a pause in the conversation with the assistant"
    return "during a conversation with the assistant"


def build_state(text: str, info: dict | None, m: Moment) -> dict:
    """The judge's whole view of it: named fields, sounds and times in words."""
    info = info or {}
    sound = {"length": _length(text)}
    loud = _loudness(info.get("level_vs_user_db"))
    if loud:
        sound["loudness"] = loud
    if info.get("stands_out") in STANDING:
        sound["room"] = STANDING[info["stands_out"]]
    state = {
        "just_heard": text,
        "assistant": {"name": m.name, **({"also_called": m.aliases} if m.aliases else {}),
                      "is": f"working on: {m.working_on}" if m.working_on else "idle, waiting for the user",
                      "face": "on the screen" if m.face_visible else "hidden from the screen"},
        "how_it_sounded": sound,
        "moment": _moment(m),
        "conversation_so_far": [{"from": who, "text": t[:LINE_LIMIT]} for who, t in m.recent[-RECENT:]],
    }
    if m.permission:
        state["permission_question"] = m.permission[:LINE_LIMIT]
    return state


def questions(m: Moment) -> dict:
    kinds = {k: v for k, v in KINDS.items() if m.permission or k not in PERMISSION_KINDS}
    return {
        "for_assistant": {
            "type": "noul",
            "instructions": f"Was just_heard said by the user to the assistant ({m.name}), for it to act on or answer?",
            "criteria": {
                "true": "Said to the assistant: a request, a question, a command, or an answer or reaction to what the "
                        "assistant just said or asked.",
                "false": "Not said to the assistant: the user talking to another person or on the phone, thinking "
                         "aloud, TV, radio or video audio, other people's conversation, filler like 'yeah', 'mm' or "
                         "'okay' that answers nothing the assistant said, or garbled words from noise."},
        },
        "kind": {
            "type": "choice",
            "instructions": "If just_heard was said to the assistant, which kind of thing is it?",
            "criteria": kinds,
        },
        "complete": {
            "type": "noul",
            "instructions": "Is just_heard a whole utterance, rather than cut off before the speaker finished?",
            "criteria": {
                "true": "Complete: a whole sentence, question or command. Short ones like 'stop', 'yes' or 'thanks' "
                        "are complete.",
                "false": "Cut off mid-sentence: it ends on words that need more after them ('can I get', 'how old "
                         "are', 'and then the')."},
        },
    }


# --- what it said, and what to do -------------------------------------------------------------------------------
@dataclass
class Verdict:
    p_for: float                                     # it was said to the assistant
    kind: str
    confidence: float                                # how sure of the kind
    p_complete: float
    probabilities: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    ms: int = 0

    @classmethod
    def from_answers(cls, data: dict) -> "Verdict":
        try:
            a = data["answers"]
            kind = a["kind"]
            return cls(p_for=float(a["for_assistant"]["noul"]), kind=str(kind["choice"]),
                       confidence=float(kind.get("confidence", 0.0)), p_complete=float(a["complete"]["noul"]),
                       probabilities={k: round(float(v), 3) for k, v in (kind.get("probabilities") or {}).items()},
                       cost_usd=float((data.get("usage") or {}).get("cost") or 0.0))
        except (KeyError, TypeError, ValueError) as e:
            raise JudgeUnavailable(f"the judge's answer was not understood ({e!r})") from None

    def summary(self) -> dict:
        return {"for_assistant": round(self.p_for, 3), "kind": self.kind, "confidence": round(self.confidence, 3),
                "complete": round(self.p_complete, 3), "ms": self.ms}


@dataclass
class Action:
    do: str        # send | drop | hold | wait | stop | approve | decline | hide | show | compact | new_conversation
    why: str


COMMANDS = {"stop": "stop", "approve": "approve", "decline": "decline", "hide_face": "hide", "show_face": "show",
            "compact": "compact", "new_conversation": "new_conversation"}


def route(v: Verdict, *, named: bool = False, standing: str | None = None, unsure: str = "hold",
          can_wait: bool = True, permission: bool = False) -> Action:
    """What to do with what was heard. ``named``: it began with the face's name (it was for the face, whatever the
    judge thinks); ``standing``: how much it stood out from the room; ``unsure``: "hold" or "send" when the judge
    can't tell; ``can_wait``: it may still be waiting for the rest of a cut-off sentence."""
    if not named and v.p_for < FOR_ME:
        if v.p_for < NOT_FOR_ME:
            return Action("drop", "not said to the assistant")
        if standing == "no" and v.p_for < NOT_FOR_ME_BACKGROUND:
            return Action("drop", "sounded like the background, and not said to the assistant")
        if unsure == "hold":
            return Action("hold", "not sure it was said to the assistant")
        return Action("send", "not sure it was said to the assistant: sent anyway")
    if can_wait and v.p_complete < CUT_OFF:
        return Action("wait", "sounds cut off: waiting for the rest")
    command = COMMANDS.get(v.kind)
    if command and command in ("approve", "decline") and not permission:
        command = None
    need = NEW_CONVERSATION if command == "new_conversation" else COMMAND
    if command and v.confidence >= need:
        return Action(command, f"heard as {v.kind.replace('_', ' ')}")
    return Action("send", v.kind.replace("_", " "))


# --- asking it ------------------------------------------------------------------------------------------------------
class Judge:
    """Asks the decision model. ``key()`` -> the OpenRouter API key or None; ``model()`` -> the model id."""

    def __init__(self, key, model):
        self.key, self.model = key, model
        self._session: aiohttp.ClientSession | None = None
        self.calls = 0
        self.spent_usd = 0.0
        self.last_error: str | None = None

    async def _post(self, body: dict, key: str) -> dict:
        if self._session is None or self._session.closed:          # kept open: no new TLS handshake each time
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S))
        try:
            async with self._session.post(URL, json=body, headers={"Authorization": f"Bearer {key}",
                                                                     "X-Title": "Wireface"}) as r:
                data = await r.json(content_type=None)
                if r.status >= 400:
                    msg = ((data or {}).get("error") or {}).get("message") if isinstance(data, dict) else None
                    raise JudgeUnavailable(f"OpenRouter said {r.status}: {msg or 'no reason given'}")
                return data
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            raise JudgeUnavailable(f"can't reach OpenRouter ({e or type(e).__name__})") from None

    async def decide(self, text: str, info: dict | None, moment: Moment) -> Verdict:
        key = self.key()
        if not key:
            raise JudgeUnavailable("there is no OpenRouter API key")
        body = {"model": self.model() or DEFAULT_MODEL, "state": build_state(text, info, moment),
                "questions": questions(moment)}
        started = time.monotonic()
        try:
            v = Verdict.from_answers(await self._post(body, key))
        except JudgeUnavailable as e:
            self.last_error = str(e)
            raise
        v.ms = round((time.monotonic() - started) * 1000)
        self.calls += 1
        self.spent_usd += v.cost_usd
        self.last_error = None
        log.info("judged %r: %s", text[:80], v.summary())
        return v

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
