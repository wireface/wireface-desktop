"""The prompts the room gives each agent. They are self-contained: an agent needs no instructions file to follow them.

Every prompt carries the lines of the conversation the agent has not seen yet, labelled by speaker, so what the
other agents said reaches it as text (and only as text: agents never hear each other).
"""
from __future__ import annotations

import re

PASS = "PASS"


def is_pass(text: str) -> bool:
    """The agent had nothing to add (it said PASS, or nothing)."""
    return re.sub(r"[^a-z]", "", (text or "").lower()) in ("", "pass")


def might_become_pass(partial: str) -> bool:
    """A reply so far that could still turn out to be "PASS": hold it back until it is clear."""
    letters = re.sub(r"[^a-z]", "", partial.lower())
    return len(letters) <= len(PASS) and PASS.lower().startswith(letters)


def label(prompt: str) -> str:
    """What the activity log shows for a prompt: the room's long prompts are shown as what they asked for."""
    if not prompt.startswith("[Room]"):
        return prompt
    if "It is your turn to comment" in prompt:
        return "Room: asked to comment on the conversation"
    if "so you have the last word" in prompt:
        return "Room: asked for the conclusion"
    users = [line for line in prompt.splitlines() if line.startswith("[User]: ")]
    return f"Room: the user asked: {users[-1][len('[User]: '):]}" if users else "Room: asked a question"


def _lines(lines: list[tuple[str, str]]) -> str:
    return "\n".join(f"[{who}]: {text}" for who, text in lines)


def _header(you: str, others: list[str]) -> str:
    peers = ", ".join(others) if others else "no one else"
    return (f"[Room] You are {you}, one of several agents the user is talking to (the others: {peers}). "
            f"The user hears everything you write aloud, so write short, natural spoken sentences: no markdown, "
            f"lists or code blocks. Messages from others are shown as [Name]: text.")


def ask_prompt(you: str, others: list[str], lines: list[tuple[str, str]], first_to_answer: bool) -> str:
    race = (" The other agents were asked too, and whoever answers first speaks first; the rest will comment "
            "afterwards, so just do your best." if first_to_answer else
            " You were asked directly; the others will comment after you.")
    return (f"{_header(you, others)}{race}\n\nConversation:\n{_lines(lines)}\n\n"
            f"Answer the user's latest message as you normally would (do the work if it needs doing).")


def contribute_prompt(you: str, others: list[str], lines: list[tuple[str, str]]) -> str:
    return (f"{_header(you, others)}\n\nConversation so far:\n{_lines(lines)}\n\n"
            f"It is your turn to comment. Add only what is new and useful (a correction, a risk, a better idea, "
            f"something the others missed) in one to three short spoken sentences. Do not repeat what was said. "
            f"If you have nothing to add, reply with exactly {PASS} and nothing else. Do not make changes to files "
            f"or run commands for this: just comment.")


def final_prompt(you: str, others: list[str], lines: list[tuple[str, str]]) -> str:
    return (f"{_header(you, others)}\n\nConversation so far:\n{_lines(lines)}\n\n"
            f"You answered first, so you have the last word. Give the user your conclusion in one or two short "
            f"spoken sentences, taking the others' comments into account. Do not start a new discussion and do not "
            f"ask the others anything.")
