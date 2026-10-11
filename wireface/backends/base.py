"""What the face needs from the agent behind it (Claude Code today; Codex, Gemini or opencode later).

A backend owns one agent session. The face gives it prompts and listens: spoken text goes to
``host.speak``, everything it does goes to ``host.activity`` (see ``activity.py``), and it asks the user's
permission through ``host.ask_permission``. Which backend a face uses, and how, is its ``backend`` config
(``profile.py``): the generic keys are ``cwd``, ``model``, ``skip_permissions``, ``permission_mode`` and
``auto_start``; anything specific to one agent goes in ``options``.

``host`` (the App) supplies:
    backend_cfg        the ``backend`` section of the profile (read it fresh: the user can change it)
    state              dict with ``session_id``, the conversation to pick up again; ``save_state()`` stores it
    activity           the ActivityLog
    speak(text)  notify(text)  changed()  ask_permission(show, say, can_always) -> (allow, always)
"""
from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path

from ..room.envelope import label

log = logging.getLogger(__name__)

# A prompt refused because the conversation no longer fits the model. Each agent and provider words it its own way:
# "Prompt is too long" (Claude Code), "Codex ran out of room in the model's context window", "This model's maximum
# context length is ...", "context_length_exceeded", "The total text input size exceeds 8 MB" (OpenCode's provider).
OVERFLOW = re.compile(
    r"prompt is too long|input is too long|context[ _-]?(length|window)|maximum context|ran out of room"
    r"|request (entity )?too large|exceeds? (the )?(maximum|max|context|token|\d+ ?[km]b\b)|token count\b.{0,40}\bexceeds?",
    re.I)


def context_overflow(message: str | None) -> bool:
    return bool(OVERFLOW.search(message or ""))


COMPACTING = "compacting the conversation"     # the op while it does (the face's eyes show a spinner)

RECAP_CHARS = 6000           # how much of the old conversation a new one is given
RECAP = """(The previous conversation grew too large for the model, so this is a new one. The latest part of it, for context:

{lines}
)

"""


def recap(activity, limit: int = RECAP_CHARS) -> str:
    """The latest exchanges in the activity log (what the user asked, what the agent replied), newest kept, as the
    opening of a new conversation; "" if there is none."""
    events = activity.recent(400)
    replies: dict[str, list[str]] = {}
    for e in events:
        if e["kind"] == "assistant" and e.get("turn_id") and not e.get("parent_id") and (e.get("output") or "").strip():
            replies.setdefault(e["turn_id"], []).append(e["output"].strip())
    clip = lambda t, n: t if len(t) <= n else t[:n] + "..."
    blocks = []
    for e in events:
        if e["kind"] == "turn" and (e.get("extra") or {}).get("source") == "user" and e["id"] in replies:
            blocks.append(f"User: {clip(label(e.get('input') or ''), 600)}\nYou: {clip(' '.join(replies[e['id']]), 900)}")
    kept, size = [], 0
    for block in reversed(blocks):
        if size + len(block) > limit:
            break
        kept.insert(0, block)
        size += len(block) + 2
    return RECAP.format(lines="\n\n".join(kept)) if kept else ""


class NoReply(RuntimeError):
    """The agent failed a prompt without saying or doing anything."""

# How the agent should write its replies: they are read aloud, not shown.
SPOKEN = """You are talking with the user through an animated face on their desktop. Everything you write in a reply is read aloud by a text-to-speech voice and is not shown as text. So:
- Answer the way a person would say it: short, natural sentences, usually one to three of them.
- No markdown, lists, tables, headings or emoji. Code blocks are skipped silently, so never rely on one.
- Don't read out code, long paths, links or command output. Say what you did or found in plain words; when the user needs something long or exact, write it to a file and say where it is.
- The user's messages often come from speech recognition: tolerate small transcription errors, and ask when a request is unclear. Ask questions in plain speech.
Only the spoken reply needs to be brief: do the work itself as thoroughly as you normally would."""

def host_hook(host, name: str, *args) -> None:
    """Tell the host something if it wants to know (a test host may not)."""
    fn = getattr(host, name, None)
    if fn:
        try:
            fn(*args)
        except Exception:
            log.exception("host.%s failed", name)


INSTRUCTIONS_FILE = "AGENTS.md"      # read by OpenCode and Codex from the folder they work in


def ensure_instructions(folder: str | Path) -> bool:
    """Create ``AGENTS.md`` in ``folder`` if there is none, telling the agent to reply the way the face speaks.
    An existing file is never touched. Returns True when it was created."""
    path = Path(folder) / INSTRUCTIONS_FILE
    if path.exists():
        return False
    try:
        path.write_text(
            "# Instructions for the face\n\n"
            "Created by Wireface because this folder had no AGENTS.md. Edit or delete it freely: it is only "
            "created when missing.\n\n" + SPOKEN + "\n", encoding="utf-8")
    except OSError as e:
        log.warning("could not create %s: %s", path, e)
        return False
    log.info("created %s", path)
    return True


class Backend(ABC):
    id = ""                                 # the ``backend.type`` this implements
    label = ""
    # what the agent can do, so the UI offers only what works: "permission_prompts", "resume", "interrupt",
    # "background_tasks", "skip_permissions", "compact" (summarise the conversation on request), "context" (it says
    # how full the context is, so the face can compact while idle: ``backend.compact_at``)
    capabilities: frozenset[str] = frozenset()
    permission_modes: dict[str, str] = {}   # id -> label, for ``backend.permission_mode``
    permission_help = "What it may do before asking."     # shown beside the setting

    def __init__(self, host):
        self.host = host
        self.inbox: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        # what the face shows (the beacon and the status line)
        self.connected = False              # the agent process is up and a session is open
        self.connecting = False
        self.active = False                 # it is working on something right now
        self.op: str | None = None          # what it is doing ("reading app.py")
        self.tasks: dict[str, str] = {}     # background jobs and subagents still running: id -> description
        self.last_error: str | None = None  # why it last stopped; cleared once it works again
        self.session_cost: float | None = None   # what the session has cost so far (USD), if the agent says
        self.context: dict | None = None    # how full the context window is, if the agent says: {"used", "size"} tokens
        # settings that need a new agent process are waiting for one (from ``reconnect()`` until it is up with them,
        # or has failed; with auto_start off, until the next prompt starts it). True until ``run`` first starts it.
        self.applying = True
        self._reconnect_queued = False

    @classmethod
    def validate(cls, cfg: dict) -> None:
        """Raise ValueError if the ``backend`` section is not valid for this agent."""
        mode = cfg.get("permission_mode")
        if cls.permission_modes and mode not in cls.permission_modes:
            raise ValueError(f"permission_mode: unknown mode {mode!r} (one of {', '.join(cls.permission_modes)})")

    # --- from the host (never blocks) ---
    def send(self, text: str):
        self.inbox.put_nowait(("user", text))

    def new_conversation(self):
        self.context = None
        self.inbox.put_nowait(("reset", ""))

    def compact(self):
        """Summarise the conversation so far, so a long one can go on (after whatever is running now)."""
        self.inbox.put_nowait(("compact", ""))

    def reconnect(self):
        """Settings that only apply to a new agent process (folder, model, skip_permissions) changed. One restart
        that hasn't begun yet picks up every change made before it does (it reads the settings when it starts)."""
        self.applying = True
        if not self._reconnect_queued:
            self._reconnect_queued = True
            self.inbox.put_nowait(("reconnect", ""))
        host_hook(self.host, "changed")

    def shutdown(self):
        """End the session from inside ``run`` (which then returns): the task that opened it must be the one to close it."""
        self.inbox.put_nowait(("quit", ""))

    @property
    def busy(self) -> bool:
        return self.active or not self.inbox.empty()

    def _drain(self):
        """Forget the prompts waiting (``halt``); a restart for new settings still happens."""
        while not self.inbox.empty():
            self.inbox.get_nowait()
        if self._reconnect_queued:
            self.inbox.put_nowait(("reconnect", ""))

    async def _start(self, restart: bool = False):
        """Open the session if the face starts with it (``restart``: close the running one first, for new settings).
        Settings changed while it started queue another restart (each agent's ``_connect`` checks)."""
        if restart:
            self._reconnect_queued = False
            await self._close()
        try:
            await self._autostart()
        finally:
            self.applying = self._reconnect_queued
            host_hook(self.host, "changed")

    # --- one prompt, and what to do when the conversation can't go on ------------------------------------------
    # Each agent provides ``_attempt(text, prompt)`` (one prompt, logged as ``text``, sent as ``prompt`` if given;
    # raises NoReply when nothing at all came back), ``_compact_session()`` (raises if it can't), ``_close()``, and
    # sets ``_resumed`` while a conversation picked up from before hasn't answered yet.
    _resumed = False

    async def _ask(self, text: str) -> None:
        """One prompt. A conversation over the model's limit is compacted and the prompt asked again; if that doesn't
        do it, the prompt goes to a new conversation that opens with a recap of this one. A conversation picked up from
        before that can't answer at all (gone, or broken) is left for a new one too."""
        try:
            await self._attempt(text)
            return
        except NoReply as e:
            error = e
        if context_overflow(str(error)):
            if await self._compact_now("too long"):
                try:
                    await self._attempt(text)
                    return
                except NoReply as e:
                    if not context_overflow(str(e)):
                        raise
            await self._start_over(text, error, with_recap=True)
        elif self._resumed:
            await self._start_over(text, error, with_recap=False)
        else:
            raise error

    async def _start_over(self, text: str, error: Exception, with_recap: bool) -> None:
        """Ask in a new conversation. It is kept only if it answers: otherwise the trouble wasn't the old one."""
        old = self.host.state.get("session_id")
        opening = recap(self.host.activity) if with_recap else ""
        log.warning("%s: conversation %s can't go on (%s); asking in a new one", self.label, old, error)
        await self._close()
        self.host.state["session_id"] = None
        self.context = None
        try:
            await self._attempt(text, opening + text if opening else None)
        except BaseException:
            if self.host.state.get("session_id") != old:
                self.host.state["session_id"] = old
                self.host.save_state()
            raise
        self.host.save_state()
        self.host.activity.emit("notice", summary="Started a new conversation" + (", with a recap of the last one" if opening else ""),
                                output=f"The last one could not go on: {error}")

    async def _compact_now(self, why: str) -> bool:
        """Summarise the conversation (``why``: "asked", "auto" or "too long"); says how it went in the activity log."""
        if not self.host.state.get("session_id"):
            self.host.activity.emit("notice", summary="Nothing to compact yet", output="The conversation hasn't started.")
            return False
        before = self.context
        self.active, self.op = True, COMPACTING
        self.host.changed()
        try:
            await self._compact_session()
        except Exception as e:
            log.warning("%s: compacting failed: %s", self.label, e)
            self.host.activity.emit("error", summary=f"Couldn't compact the conversation: {e}"[:300])
            return False
        finally:
            self.active, self.op = False, None
            self.host.changed()
        reason = {"asked": "On request.", "too long": "The conversation had grown too long for the model.",
                  "auto": "It was getting full: compacted while idle, so it doesn't happen in the middle of a reply."}[why]
        if before and before.get("size"):
            reason += f" Before: {before['used']:,} of {before['size']:,} tokens."
        self.host.activity.emit("notice", summary="Compacted the conversation", output=reason)
        log.info("%s: compacted the conversation (%s)", self.label, why)
        return True

    async def _maybe_compact(self) -> None:
        """After a reply, while nothing else is waiting: compact once the context is ``backend.compact_at`` percent full
        (the agent would otherwise do it itself later, in the middle of a reply, or run out of room)."""
        at = self.host.backend_cfg.get("compact_at") or 0
        c = self.context
        if at and c and c.get("size") and c.get("used") is not None and self.inbox.empty() \
                and c["used"] * 100 >= at * c["size"]:
            await self._compact_now("auto")

    # --- implemented by each agent ---
    @abstractmethod
    async def run(self):
        """Own the session: every use of it happens in this task."""

    @abstractmethod
    async def halt(self):
        """Stop the current work and forget prompts that were waiting behind it."""

    @abstractmethod
    async def apply_config(self):
        """The user changed ``backend`` settings: do whatever they need (reconnect, switch mode)."""

    @abstractmethod
    async def close(self):
        """End the session and the agent process."""
