"""The agent's full session, live: Claude Code's own transcript file, followed as it grows.

The activity log (``activity.py``) is the face's summary of the work, with long texts cut. Claude Code itself writes
every message of the session to ``~/.claude/projects/<working folder>/<session id>.jsonl``: prompts, its text and
reasoning, every tool call and the complete result. ``Transcript`` tails that file for whichever session the face is
on and hands the panel each new entry, so the whole session can be watched while it happens. Only reading: Claude
Code holds no lock on it, and nothing here writes to it.

The working folder named is the one the session *started* in: Claude Code goes on appending to that file when the
session is resumed somewhere else (its folder moved or renamed), so ``find_transcript`` looks in the other folders too.

An entry, as the panel gets it::

    {id, ts, kind, text, name, input, is_error, sidechain, tool_use_id, duration_ms}

kind      what it is
prompt    what the face sent Claude Code (your words, or a room's prompt)
text      what Claude wrote
thinking  its reasoning (Claude Code often keeps only that it thought, and for how long: ``duration_ms``)
tool_use  a tool call: ``name``, ``input`` (JSON text)
tool_result  the tool's complete output (``is_error``)
context   what Claude Code added to the conversation itself (environment, reminders)
cost      the session's cost so far (``text`` is the amount in USD)
title     the session's title
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

POLL_S = 0.4
BACKLOG = 400                 # entries kept for a panel that opens later
TEXT_LIMIT = 60_000           # a tool that read a big file: the rest is in the file itself
FIRST_READ = 2_000_000        # bytes read from the end of a long transcript when following starts


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def transcript_path(cwd: str, session_id: str) -> Path:
    """Where Claude Code keeps a session: its working folder with every non-alphanumeric character as "-"."""
    folder = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    return claude_dir() / "projects" / folder / f"{session_id}.jsonl"


def find_transcript(cwd: str, session_id: str) -> Path:
    """The session's file: in its working folder's, or, for a session that started in another folder, wherever it is
    (the most recently written, if there are several). Its working folder's when there is none yet (a new session)."""
    expected = transcript_path(cwd, session_id)
    if expected.exists():
        return expected
    found = []
    try:
        for folder in (claude_dir() / "projects").iterdir():
            path = folder / expected.name
            try:
                found.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except OSError:
        pass
    return max(found)[1] if found else expected


def _ts(value) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def _clip(text: str) -> str:
    if len(text) <= TEXT_LIMIT:
        return text
    return f"{text[:TEXT_LIMIT]}\n... [{len(text) - TEXT_LIMIT} more characters in the transcript file]"


def _content_text(content) -> str:
    """A tool result's or message's content (a string, or a list of text / image parts) as text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for part in content if isinstance(content, list) else [content]:
        if isinstance(part, dict):
            parts.append(part.get("text", "") if part.get("type") == "text" else f"[{part.get('type', 'part')}]")
        else:
            parts.append(str(part))
    return "\n".join(parts)


def entries(record: dict) -> list[dict]:
    """What one transcript line shows (none, one, or several entries)."""
    kind = record.get("type")
    base = {"ts": _ts(record.get("timestamp")), "sidechain": bool(record.get("isSidechain"))}
    uid = record.get("uuid") or f"{kind}-{record.get('timestamp', '')}"
    out = []

    def add(n: int, **fields):
        out.append({"id": f"{uid}:{n}", "kind": "", "text": "", "name": "", "input": None, "is_error": False,
                    "tool_use_id": None, "duration_ms": None, **base, **fields})

    message = record.get("message") or {}
    content = message.get("content")
    if kind == "user":
        if isinstance(content, str):
            add(0, kind="prompt", text=_clip(content))
        elif isinstance(content, list):
            for n, block in enumerate(content):
                if block.get("type") == "tool_result":
                    add(n, kind="tool_result", text=_clip(_content_text(block.get("content"))),
                        is_error=bool(block.get("is_error")), tool_use_id=block.get("tool_use_id"))
                elif block.get("type") == "text" and block.get("text", "").strip():
                    add(n, kind="prompt", text=_clip(block["text"]))
    elif kind == "assistant" and isinstance(content, list):
        for n, block in enumerate(content):
            bt = block.get("type")
            if bt == "text" and block.get("text", "").strip():
                add(n, kind="text", text=_clip(block["text"]))
            elif bt == "thinking":
                add(n, kind="thinking", text=_clip(block.get("thinking") or ""), duration_ms=record.get("thinkingDurationMs"))
            elif bt == "tool_use":
                add(n, kind="tool_use", name=block.get("name", ""), tool_use_id=block.get("id"),
                    input=_clip(json.dumps(block.get("input"), indent=2, ensure_ascii=False, default=str)))
    elif kind == "attachment":
        rendered = record.get("rendered")
        parts = rendered if isinstance(rendered, list) else [rendered] if rendered else []
        text = "\n".join(_content_text(p.get("content")) if isinstance(p, dict) else str(p) for p in parts)
        text = re.sub(r"</?system-reminder>", "", text).strip()
        att = record.get("attachment") or {}
        if text:
            add(0, kind="context", name=str(att.get("type", "")) if isinstance(att, dict) else "", text=_clip(text))
    elif kind == "cost-state" and record.get("totalCostUSD") is not None:
        add(0, kind="cost", text=str(record["totalCostUSD"]))
    elif kind == "ai-title" and record.get("aiTitle"):
        add(0, kind="title", text=str(record["aiTitle"]))
    return out


SEARCH_FILES = 40            # session files looked through for a tool's result


def find_tool_result(folder: Path, tool_use_id: str, first: Path | None = None) -> dict | None:
    """The result of tool call ``tool_use_id`` in a project's session files: {text, is_error, session} or None."""
    needle = tool_use_id.encode()
    try:
        others = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        others = []
    files = ([first] if first else []) + [p for p in others if p != first]
    for path in files[:SEARCH_FILES]:
        try:
            with open(path, "rb") as f:
                for raw in f:
                    if needle not in raw:
                        continue
                    try:
                        record = json.loads(raw)
                    except ValueError:
                        continue
                    for e in entries(record):
                        if e["kind"] == "tool_result" and e["tool_use_id"] == tool_use_id:
                            return {"text": e["text"], "is_error": e["is_error"], "session": path.stem}
        except OSError:
            continue
    return None


class Transcript:
    """Follows the current session's transcript. ``source()`` -> (cwd, session_id) or None (not Claude Code / no
    session yet); ``on_entries(list)`` and ``on_reset(view)`` are called on the event loop; ``secrets()`` are values to
    hide (API keys)."""

    def __init__(self, source, on_entries, on_reset, secrets=lambda: set()):
        self.source, self.on_entries, self.on_reset, self.secrets = source, on_entries, on_reset, secrets
        self.path: Path | None = None
        self.session_id: str | None = None
        self._src = None                                         # the (cwd, session id) self.path was found for
        self.backlog: collections.deque[dict] = collections.deque(maxlen=BACKLOG)
        self._offset = 0
        self._partial = b""
        self._task: asyncio.Task | None = None

    def tool_result(self, tool_use_id: str) -> dict | None:
        """A tool call's complete result (as complete as an entry gets: up to TEXT_LIMIT), for the activity log, which
        keeps only the start of long ones. Looked for in this session's file, then the folder's other sessions, newest
        first. None if it isn't in any of them."""
        src = self.source()
        if not src or not tool_use_id:
            return None
        current = find_transcript(*src)
        found = find_tool_result(current.parent, tool_use_id, first=current)
        if found:
            for s in (s for s in self.secrets() if s):
                found["text"] = found["text"].replace(s, "[hidden]")
        return found

    def view(self, limit: int = BACKLOG) -> dict:
        items = list(self.backlog)[-max(1, limit):]
        return {"available": self.path is not None, "session_id": self.session_id,
                "path": str(self.path) if self.path else None, "exists": bool(self.path and self.path.exists()),
                "entries": items}

    def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self._run(), name="transcript")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                self.poll()
            except Exception:
                log.exception("following the transcript")
            await asyncio.sleep(POLL_S)

    def poll(self) -> None:
        src = self.source()
        if src != self._src:                                 # looked for once per session, not on every poll
            path = find_transcript(*src) if src else None
            self._src, self.path, self.session_id = src, path, (src[1] if src else None)
            self.backlog.clear()
            self._offset, self._partial = 0, b""
            if path and path.exists():                       # an existing session: start from its recent end
                size = path.stat().st_size
                self._offset = max(0, size - FIRST_READ)
                self._read(skip_first_line=self._offset > 0, announce=False)
            self.on_reset(self.view())
            return
        if self.path and self.path.exists():
            self._read()

    def _read(self, skip_first_line: bool = False, announce: bool = True) -> None:
        size = self.path.stat().st_size
        if size < self._offset:                             # replaced or truncated: read it again
            self._offset, self._partial = 0, b""
        if size == self._offset:
            return
        with open(self.path, "rb") as f:
            f.seek(self._offset)
            data = f.read(size - self._offset)
        self._offset = size
        lines = (self._partial + data).split(b"\n")
        self._partial = lines.pop()                          # a line still being written
        if skip_first_line and lines:
            lines = lines[1:]
        new = []
        hide = [s for s in self.secrets() if s]
        for raw in lines:
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            for e in entries(record):
                for s in hide:
                    for key in ("text", "input"):
                        if e[key] and s in e[key]:
                            e[key] = e[key].replace(s, "[hidden]")
                new.append(e)
        self.backlog.extend(new)
        if new and announce:
            self.on_entries(new)
