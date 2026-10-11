"""What every agent backend does with a turn: speak the reply, log the work.

``TurnReporter`` is mixed into a ``Backend``. The agent-specific code translates its own events into these few
calls (``begin_turn`` / ``say_delta`` / ``tool_start`` / ``tool_end`` / ``end_turn``...) and the beacon, the
spoken reply and the activity panel follow.
"""
from __future__ import annotations

import json
import time

from ..room.envelope import label
from ..speech import SpeechStream
from .base import host_hook

TEXT_UPDATE_S = 0.25         # how often a streaming reply or reasoning is pushed to the activity log


def pretty(value) -> str | None:
    """A tool's input as readable text."""
    if value in (None, "", {}, []):
        return None
    return value if isinstance(value, str) else json.dumps(value, indent=2, default=str, ensure_ascii=False)


class TurnReporter:
    """Needs ``self.host`` (the App: ``speak``, ``notify``, ``changed``, ``activity``)."""

    def _init_turns(self):
        self._speech = SpeechStream()
        self._turn_id: str | None = None
        self._text_id: str | None = None             # the activity event of the reply being streamed
        self._text, self._text_msg, self._text_at = "", None, 0.0
        self._think_id: str | None = None
        self._think, self._think_at = "", 0.0

    @property
    def log(self):
        return self.host.activity

    # --- turns ---
    def begin_turn(self, text: str, source: str = "user") -> None:
        text = label(text)
        self._turn_source = source
        self._turn_id = self.log.emit("turn", summary=text[:200] or "Background activity", input=text,
                                      status="running", extra={"source": source}).id
        host_hook(self.host, "turn_started", source)

    def end_turn(self, *, failed: bool = False, interrupted: bool = False, duration_ms: int | None = None, **extra) -> None:
        """Close the turn: speak what is left of the reply, close any steps still open, record how it went."""
        self.end_text()
        self.end_thinking()
        had_turn = bool(self._turn_id)
        if self._turn_id:
            self.log.finish_running(self._turn_id, "(interrupted)" if interrupted else "(no result)")
            fields = {"status": "error" if failed else "ok", "extra": {"interrupted": interrupted, **extra}}
            if duration_ms is not None:
                fields["duration_ms"] = duration_ms
            self.log.update(self._turn_id, **fields)
        self._turn_id = None
        if had_turn:
            host_hook(self.host, "turn_done", getattr(self, "_turn_source", "user"))

    # --- what it says ---
    def say_delta(self, delta: str, message_id: str | None = None) -> None:
        """Part of a reply as it streams: spoken sentence by sentence, and shown as one growing log entry."""
        if message_id != self._text_msg and self._text_id is not None:
            self.end_text()
        self._text_msg = message_id
        self._text += delta
        now = time.monotonic()
        if self._text_id is None:
            self._text_id = self.log.emit("assistant", turn_id=self._turn_id, status="running", output=self._text).id
            self._text_at = now
        elif now - self._text_at >= TEXT_UPDATE_S:
            self._text_at = now
            self.log.update(self._text_id, output=self._text)
        self.host.speak(self._speech.feed(delta))

    def end_text(self) -> None:
        self.host.speak(self._speech.flush())
        if self._text_id is not None:
            self.log.update(self._text_id, output=self._text, status="ok")
        self._text_id, self._text, self._text_msg = None, "", None

    def say_whole(self, text: str) -> None:
        """A reply that arrived complete."""
        self.end_text()
        if text.strip():
            self.log.emit("assistant", turn_id=self._turn_id, output=text)
            self.host.speak(text)

    def think_delta(self, delta: str) -> None:
        self._think += delta
        now = time.monotonic()
        if self._think_id is None:
            self._think_id = self.log.emit("thinking", turn_id=self._turn_id, status="running", output=self._think).id
            self._think_at = now
        elif now - self._think_at >= TEXT_UPDATE_S:
            self._think_at = now
            self.log.update(self._think_id, output=self._think)

    def end_thinking(self) -> None:
        if self._think_id is not None:
            self.log.update(self._think_id, output=self._think, status="ok")
        self._think_id, self._think = None, ""

    # --- what it does ---
    def tool_start(self, id: str, name: str, summary: str = "", input=None, parent_id: str | None = None, **extra) -> None:
        self.end_text()                                  # what it said before the call is complete
        self.end_thinking()
        if self.log.get(id) is None:
            self.log.emit("tool", id=id, turn_id=self._turn_id, parent_id=parent_id, name=name, summary=summary,
                          status="running", input=pretty(input), extra=extra)
        else:
            self.tool_update(id, name=name, summary=summary, input=input)

    def tool_update(self, id: str, **fields) -> None:
        clean = {}
        for key, value in fields.items():
            if key == "input":
                value = pretty(value)
            if value not in (None, ""):
                clean[key] = value
        if clean:
            self.log.update(id, **clean)

    def tool_end(self, id: str, ok: bool, output=None, **fields) -> None:
        if self.log.get(id) is None:
            self.log.emit("tool", id=id, turn_id=self._turn_id, status="running")
        self.log.update(id, status="ok" if ok else "error", **({"output": pretty(output)} if pretty(output) else {}), **fields)

    # --- asides ---
    def note(self, text: str, detail: str | None = None, id: str | None = None) -> None:
        if id and self.log.get(id):
            self.log.update(id, summary=text, **({"output": detail} if detail else {}))
        else:
            self.log.emit("notice", id=id, turn_id=self._turn_id, summary=text, **({"output": detail} if detail else {}))

    def problem(self, text: str) -> None:
        self.log.emit("error", turn_id=self._turn_id, summary=text[:300])
