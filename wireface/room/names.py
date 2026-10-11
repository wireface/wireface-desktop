"""Names and echo: working out who the user is talking to, and whether the microphone is hearing the user or just the
agents' own speech coming out of the speakers. Pure functions.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

# How speech recognition tends to write the agents' names, beyond the plain name.
VARIANTS = {
    "claude": ["claude", "claud", "clod"],
    "codex": ["codex", "code x", "codecs", "kodex"],
    "opencode": ["opencode", "open code", "open-code"],
    "gemini": ["gemini"],
}
FILLERS = {"hey", "hi", "hello", "ok", "okay", "yo", "so", "right", "and", "now", "um", "uh"}
EVERYONE = ("everyone", "everybody", "all of you", "all")
STOP_WORDS = ("stop", "wait", "quiet", "enough", "hold on", "be quiet", "shut up", "pause")
STOP_TOKENS = {w for phrase in STOP_WORDS for w in phrase.split()} | {"please", "now", "a", "second", "moment", "it", "up"}


def words(text: str) -> list[str]:
    """Lowercase words, no punctuation."""
    return re.findall(r"[a-z0-9']+", text.lower().replace("-", " "))


def norm(text: str) -> str:
    return " ".join(words(text))


def aliases_for(name: str, kind: str = "", extra: list[str] | None = None) -> list[str]:
    """Everything that should count as saying this agent's name, longest first."""
    found = {norm(name)} | {norm(a) for a in VARIANTS.get(kind, [])} | {norm(a) for a in VARIANTS.get(norm(name), [])}
    found |= {norm(a) for a in (extra or [])}
    return sorted((a for a in found if a), key=lambda a: (-len(a), a))


def _find(text: str, alias: str) -> list[int]:
    """Word positions in ``text`` where ``alias`` (one or more words) occurs."""
    tokens, target = words(text), alias.split()
    n = len(target)
    return [i for i in range(len(tokens) - n + 1) if tokens[i:i + n] == target]


def find_names(text: str, members: dict[str, list[str]]) -> list[tuple[int, str]]:
    """(word position, member id) for every mention of a member's name, in the order spoken. ``members`` maps a
    member id to its aliases."""
    out = []
    for member, aliases in members.items():
        for alias in aliases:
            out += [(i, member) for i in _find(text, alias)]
    return sorted(set(out))


def count_names(text: str, members: dict[str, list[str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _, member in find_names(text, members):
        counts[member] = counts.get(member, 0) + 1
    return counts


@dataclass(frozen=True)
class Directed:
    member: str | None        # the agent addressed, or None
    everyone: bool            # "everyone, ..."
    text: str                 # what was said after the name


def parse_directed(text: str, members: dict[str, list[str]]) -> Directed:
    """"Codex, what do you think?" -> Directed("codex-id", False, "what do you think?"). Only a name at the start
    counts (after "hey" / "ok" and the like): a name in the middle of a sentence is just a word in it."""
    tokens = words(text)
    i = 0
    while i < len(tokens) and tokens[i] in FILLERS and i < 3:
        i += 1
    rest = tokens[i:]
    for member, aliases in members.items():
        for alias in aliases:
            a = alias.split()
            if rest[:len(a)] == a:
                return Directed(member, False, " ".join(rest[len(a):]))
    for word in EVERYONE:
        a = word.split()
        if rest[:len(a)] == a:
            return Directed(None, True, " ".join(rest[len(a):]))
    return Directed(None, False, " ".join(tokens))


# --- echo ----------------------------------------------------------------------------------------------------------
def echo_ratio(heard: str, spoken: str) -> float:
    """How much of what the microphone heard is the words that were just spoken aloud: 1.0 is a perfect echo."""
    h, s = words(heard), words(spoken)
    if not h or not s:
        return 0.0
    vocab = set(s)
    in_vocab = sum(1 for w in h if w in vocab) / len(h)
    order = difflib.SequenceMatcher(None, h, s, autojunk=False).find_longest_match(0, len(h), 0, len(s)).size / len(h)
    return max(in_vocab * 0.85, order)


def _count(text: str, phrase: str) -> int:
    return len(_find(text, norm(phrase)))


@dataclass(frozen=True)
class Heard:
    kind: str                 # "echo" | "interrupt" | "ignore"
    member: str | None = None # the agent named, for an interrupt
    stop: bool = False
    rest: str = ""            # what followed the name, if it is the user's own words


ECHO = 0.75


def classify(heard: str, spoken: str, members: dict[str, list[str]]) -> Heard:
    """The microphone heard ``heard`` while ``spoken`` was being said aloud. Is it the agents' echo, the user
    interrupting (by name, or "stop"), or something to ignore?

    A name or a stop word only counts if it is said more often than the agents' own words contain it: when Codex
    says "Claude thinks...", the echo of that must not stop Claude."""
    if not words(heard):
        return Heard("ignore")
    extra_names = {m: n - count_names(spoken, {m: members[m]}).get(m, 0) for m, n in count_names(heard, members).items()}
    named = [(pos, m) for pos, m in find_names(heard, members) if extra_names.get(m, 0) > 0]
    stop = any(_count(heard, w) > _count(spoken, w) for w in STOP_WORDS)
    if named:
        pos, member = named[0]
        tokens = words(heard)
        alias_len = max((len(a.split()) for a in members[member] if _find(heard, a)), default=1)
        rest = " ".join(tokens[pos + alias_len:])
        mine = rest if echo_ratio(rest, spoken) < 0.5 else ""        # the user's words, not leftovers of the echo
        if all(w in STOP_TOKENS for w in words(mine)):                # "Codex, stop": that is not a question
            mine = ""
        return Heard("interrupt", member, stop, mine)
    if stop:
        return Heard("interrupt", None, True)
    if echo_ratio(heard, spoken) >= ECHO:
        return Heard("echo")
    return Heard("ignore")
