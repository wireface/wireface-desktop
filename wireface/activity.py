"""What the agent is doing, as a list of events the activity panel shows.

Backends describe their work in these terms whatever agent is behind them, so the panel needs no knowledge of
Claude Code, Codex or anything else. An event is created once and then updated in place (a tool call goes from
"running" to "ok" or "error" when its result arrives), so events are identified by ``id``.

kind     what it is
turn     one request and everything done for it (``input`` is the request; ``extra`` has cost / turns / tokens)
assistant  text the agent wrote (``output``)
thinking   its reasoning, when the backend reports it (``output``)
tool     a tool call: ``name`` is the tool, ``summary`` its target, ``input`` / ``output`` the details
task     a subagent or background job (``summary`` is what it is doing)
permission  a request for the user's say-so
notice   something for the user to know
error    something that went wrong
voice    what the realtime voice said to the user (``name`` "face"), or heard from them and kept for itself
         (``name`` "user": a yes / no that answered a permission question); ``output`` is the words
heard    what the judge (``judge.py``) made of something the microphone heard: ``output`` is the words, ``name`` and
         ``extra.action`` what was done (send, drop, hold, stop, ...), ``extra`` also has the judge's answers and how
         it sounded; ``extra.sent`` once the user sent on one that was held back or dropped
"""
from __future__ import annotations

import collections
import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

KINDS = ("turn", "assistant", "thinking", "tool", "task", "permission", "notice", "error", "voice", "phone", "heard")
STATUSES = ("running", "ok", "error")

INPUT_LIMIT = 4_000
OUTPUT_LIMIT = 8_000
TEXT_LIMIT = 20_000          # an assistant reply can be long


def clip(text: str | None, limit: int) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return f"{text[:limit]}\n... [{len(text) - limit} more characters]"


@dataclass
class ActivityEvent:
    id: str
    kind: str
    ts: float = field(default_factory=time.time)
    turn_id: str | None = None          # the turn this belongs to
    parent_id: str | None = None        # a subagent's work sits under the task that started it
    name: str = ""                      # tool name, task type, ...
    summary: str = ""                   # one line for the list
    input: str | None = None
    output: str | None = None
    status: str = "ok"
    duration_ms: int | None = None
    extra: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return asdict(self)


class ActivityLog:
    """The most recent events (also appended to a JSONL file per day), with listeners told of every change."""

    def __init__(self, directory: Path | None = None, limit: int = 2000):
        self.directory = directory
        self._events: collections.OrderedDict[str, ActivityEvent] = collections.OrderedDict()
        self._limit = limit
        self.listeners: list[Callable[[ActivityEvent], None]] = []
        self.secrets: set[str] = set()             # values that must never be written (API keys)

    # --- writing ---
    def emit(self, kind: str, *, id: str | None = None, **fields) -> ActivityEvent:
        if kind not in KINDS:
            raise ValueError(f"unknown activity kind {kind!r}")
        ev = ActivityEvent(id=id or secrets.token_hex(5), kind=kind)
        self._apply(ev, fields)
        self._events[ev.id] = ev
        while len(self._events) > self._limit:
            self._events.popitem(last=False)
        self._changed(ev)
        return ev

    def update(self, id: str, **fields) -> ActivityEvent | None:
        ev = self._events.get(id)
        if ev is None:
            return None
        self._apply(ev, fields)
        if ev.status != "running" and ev.duration_ms is None:
            ev.duration_ms = max(0, round((time.time() - ev.ts) * 1000))
        self._changed(ev)
        return ev

    def finish_running(self, turn_id: str, note: str) -> None:
        """A turn ended with some of its steps still open (it was interrupted): close them."""
        for ev in list(self._events.values()):
            if ev.turn_id == turn_id and ev.status == "running" and ev.kind in ("tool", "permission", "assistant"):
                if ev.kind == "assistant":
                    self.update(ev.id, status="ok")
                else:
                    self.update(ev.id, status="error", output=ev.output or note)

    def _apply(self, ev: ActivityEvent, fields: dict) -> None:
        for key, value in fields.items():
            if key == "extra":
                ev.extra.update(value)
            elif key in ("input", "output", "summary", "name") and isinstance(value, str):
                limit = TEXT_LIMIT if ev.kind in ("assistant", "thinking", "voice") and key == "output" else (
                    INPUT_LIMIT if key == "input" else OUTPUT_LIMIT)
                setattr(ev, key, clip(self._redact(value), limit if key != "summary" else 300))
            elif hasattr(ev, key) and key not in ("id", "kind"):
                setattr(ev, key, value)
            else:
                raise ValueError(f"unknown activity field {key!r}")
        if ev.status not in STATUSES:
            raise ValueError(f"unknown activity status {ev.status!r}")

    def _redact(self, text: str) -> str:
        for secret in self.secrets:
            if secret and secret in text:
                text = text.replace(secret, "[hidden]")
        return text

    def _changed(self, ev: ActivityEvent) -> None:
        self._append(ev)
        for listener in list(self.listeners):
            try:
                listener(ev)
            except Exception:                      # a broken listener must not stop the agent
                log.exception("activity listener failed")

    def _append(self, ev: ActivityEvent) -> None:
        """Running events are only kept in memory; each is written once, when it finishes."""
        if not self.directory or ev.status == "running":
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / f"activity-{time.strftime('%Y-%m-%d', time.localtime(ev.ts))}.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev.to_json(), default=str) + "\n")
        except OSError:
            log.exception("could not write the activity log")

    # --- reading ---
    def recent(self, limit: int = 500) -> list[dict]:
        events = list(self._events.values())[-max(1, limit):]
        return [e.to_json() for e in events]

    def load_history(self, limit: int = 300) -> int:
        """Bring back the latest events from the daily files (what the face did before it was restarted).
        They are not announced to listeners and not written again; returns how many were loaded."""
        if not self.directory or not self.directory.is_dir():
            return 0
        rows: dict[str, dict] = {}
        for path in sorted(self.directory.glob("activity-*.jsonl"), reverse=True):
            try:
                with open(path, encoding="utf-8") as f:
                    lines = collections.deque(f, maxlen=limit)
            except OSError:
                continue
            for line in lines:
                try:
                    row = json.loads(line)
                    rows[row["id"]] = row                 # an event written twice: the later line wins
                except (ValueError, KeyError, TypeError):
                    continue
            if len(rows) >= limit:
                break
        fields = {f for f in ActivityEvent.__dataclass_fields__}
        for row in sorted(rows.values(), key=lambda r: r.get("ts", 0))[-limit:]:
            try:
                ev = ActivityEvent(**{k: v for k, v in row.items() if k in fields})
            except TypeError:
                continue
            if ev.status == "running":                     # it never finished: the process went away
                ev.status = "error"
            self._events[ev.id] = ev
        return len(self._events)

    def get(self, id: str) -> ActivityEvent | None:
        return self._events.get(id)

    def clear(self) -> None:
        self._events.clear()
