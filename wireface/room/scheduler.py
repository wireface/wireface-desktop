"""The turn rules of a room, as a state machine with no I/O: events in, actions out.

A room is several agents (each behind its own face) sharing one user. Only one may speak at a time, and a question
is answered in a fixed shape, so they cannot talk over each other or loop:

    LEAD      the question goes to the agents (all of them racing, or only the one that was addressed). The first
              to have something to say takes the floor and speaks; it is the lead. The others keep working but
              their answers are held (they stay in those agents' own context, unspoken).
    CONTRIB   each other agent in turn gets the floor and the transcript so far, and adds something new, or passes.
    FINAL     if anyone added something, the lead hears it and gives the conclusion.

Nothing an agent says starts anything: only ``user_says`` starts a round. Every phase has a time limit, and a round
ends after one pass over the members.

The caller (``hub.py``) turns events from the faces and the microphone into method calls and carries out the
returned actions. ``clock`` is injectable for tests.
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Limits:
    contribute_s: float = 90.0       # one contribution, thinking and speaking
    final_s: float = 90.0
    round_s: float = 600.0           # a whole round
    history: int = 6                 # lines of earlier conversation a member that joins is shown


# --- actions: what the caller must do --------------------------------------------------------------------------
@dataclass(frozen=True)
class Action:
    pass


@dataclass(frozen=True)
class Ask(Action):
    """Give ``member`` the user's question. ``floor``: it may speak as soon as it has something (it was addressed);
    otherwise it must wait for a GrantFloor (it is racing the others)."""
    member: str
    round: int
    floor: bool


@dataclass(frozen=True)
class GrantFloor(Action):
    member: str
    round: int | None                # None: speech outside a round


@dataclass(frozen=True)
class Contribute(Action):
    member: str
    round: int


@dataclass(frozen=True)
class Final(Action):
    member: str
    round: int


@dataclass(frozen=True)
class Halt(Action):
    member: str | None               # None: everyone stops talking and working


@dataclass(frozen=True)
class Notice(Action):
    text: str


# --- the shared conversation ----------------------------------------------------------------------------------
class Transcript:
    """Everything said in the room, as (speaker, text). Each member has a cursor: the next prompt it gets carries
    the lines it has not seen yet, so an agent that sat a round out catches up on its next turn."""

    def __init__(self):
        self.lines: list[tuple[str, str]] = []
        self.cursor: dict[str, int] = {}

    def add(self, speaker: str, text: str, member: str | None = None) -> None:
        """``member``: the agent that said it, which has of course already seen its own words."""
        if text.strip():
            before = len(self.lines)
            self.lines.append((speaker, text.strip()))
            if member is not None and self.cursor.get(member, 0) == before:
                self.cursor[member] = before + 1

    def take(self, member: str) -> list[tuple[str, str]]:
        """The lines ``member`` has not seen; they now count as seen."""
        start = self.cursor.get(member, 0)
        self.cursor[member] = len(self.lines)
        return self.lines[start:]

    def peek(self, member: str) -> list[tuple[str, str]]:
        return self.lines[self.cursor.get(member, 0):]


@dataclass
class Round:
    id: int
    question: str
    started: float
    exclusive: bool                                      # only the addressed agent was asked
    phase: str = "lead"                                  # lead | contrib | final
    lead: str | None = None
    asked: set = field(default_factory=set)
    drafts: dict = field(default_factory=dict)           # held answers of agents that were not the lead
    draft_order: list = field(default_factory=list)      # who finished first
    lead_replied: bool = False
    lead_spoke: bool = False
    queue: list = field(default_factory=list)            # contributors still to come
    current: str | None = None
    current_replied: bool = False
    current_spoke: bool = False
    deadline: float | None = None
    contributions: list = field(default_factory=list)    # (member, text) that were spoken


class Scheduler:
    def __init__(self, lead: str = "first", limits: Limits = Limits(), clock: Callable[[], float] = time.monotonic):
        self.pinned = lead                               # "first" (whoever answers first) or an agent's name
        self.limits = limits
        self.clock = clock
        self.members: dict[str, str] = {}                # id -> name, in the order they joined
        self.transcript = Transcript()
        self.round: Round | None = None
        self.floor: str | None = None                    # the member that may speak now
        self._waiting: list[str] = []                    # members that want to speak outside a round
        self._ids = itertools.count(1)

    # --- membership ---
    def name(self, member: str) -> str:
        return self.members.get(member, member)

    def join(self, member: str, name: str) -> list[Action]:
        self.members[member] = name
        self.transcript.cursor[member] = max(0, len(self.transcript.lines) - self.limits.history)
        return []

    def leave(self, member: str) -> list[Action]:
        self.members.pop(member, None)
        self.transcript.cursor.pop(member, None)
        if member in self._waiting:
            self._waiting.remove(member)
        acts: list[Action] = []
        if self.floor == member:
            self.floor = None
        r = self.round
        if r is None:
            return acts + self._grant_waiting()
        r.asked.discard(member)
        if member in r.queue:
            r.queue.remove(member)
        if r.phase == "lead":
            if member == r.lead:
                return acts + self._finish("the lead left, ending the round")
            if r.lead is None and not r.asked:
                return acts + self._finish("everyone left")
            if r.lead is None and all(m in r.drafts for m in r.asked):
                return acts + self._finish("nobody had anything to say")
        elif member == r.current or member == r.lead:
            if member == r.lead:
                return acts + self._finish("the lead left, ending the round")
            r.current_replied = r.current_spoke = True
            return acts + self._check_current_done()
        return acts

    # --- the user ---
    def user_says(self, text: str, directed: str | None = None, everyone: bool = False) -> list[Action]:
        """A new question. Ends any round in progress. ``directed``: the id of the agent addressed by name (or whose
        face the user typed into); it leads alone. ``everyone``: ignore any pinned lead."""
        acts: list[Action] = self.interrupt() if self.round else []
        if not self.members:
            return acts + [Notice("no agents are in the room")]
        self.transcript.add("User", text)
        lead = directed if directed in self.members else None
        if lead is None and not everyone and self.pinned != "first":
            lead = next((m for m, n in self.members.items() if n.lower() == self.pinned.lower()), None)
        if lead is None and len(self.members) == 1:
            lead = next(iter(self.members))
        r = self.round = Round(next(self._ids), text, self.clock(), exclusive=lead is not None)
        if lead:
            r.lead, r.asked, self.floor = lead, {lead}, lead
            return acts + [Ask(lead, r.id, floor=True)]
        r.asked = set(self.members)
        return acts + [Ask(m, r.id, floor=False) for m in self.members]

    def interrupt(self) -> list[Action]:
        """Stop everything: the user spoke, or said a name."""
        self.round, self.floor, self._waiting = None, None, []
        return [Halt(None), Notice("interrupted")]

    # --- the members ---
    def claim(self, member: str, round: int | None = None) -> list[Action]:
        """``member`` has something to say. In a race the first claim wins the floor and the lead. ``round``: the
        round it is answering (a late message about an earlier round is ignored)."""
        r = self.round
        if r is None or (round is not None and round != r.id) or r.phase != "lead" or member not in r.asked:
            return []
        if r.lead is None:
            return self._take_lead(member)
        return []

    def reply(self, member: str, text: str, passed: bool = False, round: int | None = None,
              kind: str | None = None) -> list[Action]:
        """``member`` finished answering a prompt. ``text`` is what it said (spoken text only). ``kind``: which prompt
        it answered ("ask", "contribute" or "final"): an agent can still be busy with the question when its turn to
        comment comes, and the end of that earlier turn must not be taken for its comment."""
        r = self.round
        text = (text or "").strip()
        spoken = bool(text) and not passed
        if r is None or (round is not None and round != r.id):
            return []
        if kind == "ask" and r.phase != "lead":                 # a late answer to the question: held, never spoken
            if member in r.asked:
                r.drafts[member] = text
            return []
        if kind in ("contribute", "final") and kind != ("contribute" if r.phase == "contrib" else r.phase):
            return []
        if r.phase == "lead":
            if member not in r.asked:
                return []
            acts: list[Action] = []
            if r.lead is None:
                if not spoken:
                    r.drafts[member] = ""
                    r.draft_order.append(member)
                    if all(m in r.drafts for m in r.asked):
                        return self._finish("nobody had anything to say")
                    return []
                acts += self._take_lead(member)          # it answered without claiming first: it speaks now
            if member == r.lead:
                r.lead_replied = True
                if spoken:
                    self.transcript.add(self.name(member), text, member)
                else:
                    r.lead_spoke = True
                return acts + self._check_lead_done()
            r.drafts[member] = text
            r.draft_order.append(member)
            return acts
        if member == r.current and not r.current_replied:
            r.current_replied = True
            if spoken:
                self.transcript.add(self.name(member), text, member)
                if r.phase == "contrib":
                    r.contributions.append((member, text))
            else:
                r.current_spoke = True
            return self._check_current_done()
        return []

    def speech_done(self, member: str) -> list[Action]:
        """``member``'s voice has finished speaking (the floor is free)."""
        if member != self.floor:
            return []
        self.floor = None
        r = self.round
        if r is None:
            return self._grant_waiting()
        if r.phase == "lead" and member == r.lead:
            r.lead_spoke = True
            return self._check_lead_done()
        if member == r.current:
            r.current_spoke = True
            return self._check_current_done()
        return []

    def floor_request(self, member: str) -> list[Action]:
        """``member`` wants to speak outside any question (a background task finished): it waits for a quiet moment."""
        if self.round is None and self.floor is None:
            self.floor = member
            return [GrantFloor(member, None)]
        if member not in self._waiting:
            self._waiting.append(member)
        return []

    def tick(self) -> list[Action]:
        """Time limits."""
        r = self.round
        if r is None:
            return []
        now = self.clock()
        if now - r.started > self.limits.round_s:
            return [Halt(None)] + self._finish("the round took too long")
        if r.deadline is not None and now > r.deadline:
            who = r.current
            if r.phase == "contrib" and who:
                r.current_replied = r.current_spoke = True
                self.floor = None
                return [Halt(who), Notice(f"{self.name(who)} took too long")] + self._next()
            if r.phase == "final" and who:
                return [Halt(who)] + self._finish("the conclusion took too long")
        return []

    # --- internals ---
    def _take_lead(self, member: str) -> list[Action]:
        r = self.round
        r.lead, self.floor = member, member
        return [Notice(f"{self.name(member)} leads"), GrantFloor(member, r.id)]

    def _check_lead_done(self) -> list[Action]:
        r = self.round
        if r.lead_replied and r.lead_spoke:
            return self._start_contrib()
        return []

    def _start_contrib(self) -> list[Action]:
        r = self.round
        others = [m for m in self.members if m != r.lead]
        first = [m for m in r.draft_order if m in others]
        r.queue = first + [m for m in others if m not in first]
        r.phase = "contrib"
        return self._next()

    def _next(self) -> list[Action]:
        r = self.round
        while r.queue:
            member = r.queue.pop(0)
            if member not in self.members:
                continue
            r.current, r.current_replied, r.current_spoke = member, False, False
            r.deadline = self.clock() + self.limits.contribute_s
            self.floor = member
            return [Notice(f"{self.name(member)} adds input"), Contribute(member, r.id)]
        if r.contributions and r.lead in self.members:
            r.phase, r.current = "final", r.lead
            r.current_replied, r.current_spoke = False, False
            r.deadline = self.clock() + self.limits.final_s
            self.floor = r.lead
            return [Notice(f"{self.name(r.lead)} concludes"), Final(r.lead, r.id)]
        return self._finish()

    def _check_current_done(self) -> list[Action]:
        r = self.round
        if not (r.current_replied and r.current_spoke):
            return []
        return self._next() if r.phase == "contrib" else self._finish()

    def _finish(self, note: str | None = None) -> list[Action]:
        self.round, self.floor = None, None
        return ([Notice(note)] if note else []) + self._grant_waiting()

    def _grant_waiting(self) -> list[Action]:
        while self._waiting:
            member = self._waiting.pop(0)
            if member in self.members:
                self.floor = member
                return [GrantFloor(member, None)]
        return []

    def status(self) -> dict:
        r = self.round
        return {"phase": r.phase if r else "idle", "lead": self.name(r.lead) if r and r.lead else None,
                "floor": self.name(self.floor) if self.floor else None, "members": list(self.members.values())}
