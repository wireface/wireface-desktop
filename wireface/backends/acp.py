"""Agents that speak the Agent Client Protocol (ACP): JSON-RPC over the agent's stdin and stdout.

``AcpClient`` is the transport. ``AcpBackend`` is a face backend for any ACP agent; ``OpenCodeBackend`` is
OpenCode (``opencode acp``). The protocol, as OpenCode speaks it:

    client -> initialize, session/new | session/load, session/prompt, session/cancel (a notification)
    agent  -> session/update notifications: agent_message_chunk, agent_thought_chunk, tool_call,
              tool_call_update, plan, usage_update, session_info_update, ...
    agent  -> session/request_permission (a request: answered with the id of one of the offered options)
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import subprocess
import time
from pathlib import Path

from .base import COMPACTING, Backend, NoReply, ensure_instructions
from .exe import CREATE_NO_WINDOW, AgentNotFound, find_executable, kill_tree
from .turns import TurnReporter

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1


class AcpError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, data=None):
        super().__init__(message)
        self.code, self.data = code, data


class AcpClient:
    """One agent process and the JSON-RPC conversation with it.

    ``on_notify(method, params)`` is called (in the loop) for each notification; ``on_request(method, params)`` is
    awaited for each request from the agent and returns its result (or raises AcpError); ``on_exit(reason)`` is
    called once when the process ends on its own.
    """

    def __init__(self, argv, cwd, env, on_notify, on_request, on_exit):
        self.argv, self.cwd, self.env = argv, cwd, env
        self.on_notify, self.on_request, self.on_exit = on_notify, on_request, on_exit
        self.proc: asyncio.subprocess.Process | None = None
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._tasks: list[asyncio.Task] = []
        self._closing = False

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, cwd=self.cwd, env=self.env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=32 * 1024 * 1024, creationflags=CREATE_NO_WINDOW)
        self._tasks = [asyncio.create_task(self._read(), name="acp-read"), asyncio.create_task(self._errors(), name="acp-err")]

    # --- sending ---
    def _write(self, obj: dict) -> None:
        if self.proc is None or self.proc.stdin is None or self.proc.stdin.is_closing():
            raise AcpError("the agent is not running")
        self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))

    async def call(self, method: str, params: dict, timeout: float | None = None):
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            await self.proc.stdin.drain()
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    def notify(self, method: str, params: dict) -> None:
        try:
            self._write({"jsonrpc": "2.0", "method": method, "params": params})
        except AcpError:
            pass

    # --- receiving ---
    async def _errors(self) -> None:
        async for line in self.proc.stderr:
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self.stderr_tail.append(text)
                log.debug("agent: %s", text[:300])

    async def _read(self) -> None:
        reason = "the agent ended the session"
        try:
            async for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.debug("agent said (not JSON): %s", line[:200])
                    continue
                self._dispatch(msg)
            code = await self.proc.wait()
            if code:
                reason = f"the agent exited with code {code}" + (f": {self.stderr_tail[-1]}" if self.stderr_tail else "")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("reading from the agent")
            reason = f"reading from the agent failed: {e}"
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(AcpError(reason))
        if not self._closing:
            self.on_exit(reason)

    def _dispatch(self, msg: dict) -> None:
        if "method" in msg and "id" in msg:
            self._tasks.append(asyncio.create_task(self._answer(msg), name="acp-request"))
        elif "method" in msg:
            try:
                self.on_notify(msg["method"], msg.get("params") or {})
            except Exception:
                log.exception("handling %s", msg.get("method"))
        elif "id" in msg:
            fut = self._pending.get(msg["id"])
            if fut and not fut.done():
                err = msg.get("error")
                if err:
                    fut.set_exception(AcpError(str(err.get("message") or err), err.get("code"), err.get("data")))
                else:
                    fut.set_result(msg.get("result") or {})

    async def _answer(self, msg: dict) -> None:
        try:
            result = await self.on_request(msg["method"], msg.get("params") or {})
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        except AcpError as e:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": e.code or -32603, "message": str(e)}}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("answering %s", msg.get("method"))
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}}
        try:
            self._write(reply)
        except AcpError:
            pass

    async def close(self) -> None:
        self._closing = True
        if self.proc and self.proc.returncode is None:
            kill_tree(self.proc.pid)
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                pass
        for t in self._tasks:
            t.cancel()
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(AcpError("the session was closed"))


# --- reading ACP's descriptions of work ----------------------------------------------------------------------------
def acp_target(update: dict) -> str:
    """The one-line subject of a tool call: its file, command, pattern or address."""
    locations = update.get("locations") or []
    if locations and isinstance(locations[0], dict) and locations[0].get("path"):
        return str(locations[0]["path"])
    raw = update.get("rawInput")
    if isinstance(raw, dict):
        for key in ("command", "filePath", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
            if raw.get(key):
                return str(raw[key]).strip().splitlines()[0][:200]
    return ""


def acp_op(title: str, kind: str | None, target: str) -> str:
    name = os.path.basename(target) if target else ""
    if kind == "edit":
        return f"editing {name}" if name else "editing"
    if kind == "read":
        return f"reading {name}" if name else "reading"
    return {"execute": "running a command", "search": "searching", "fetch": "looking online",
            "delete": "deleting a file", "move": "moving a file", "think": "thinking"}.get(kind or "", f"using {title}")


def acp_request_text(tool_call: dict) -> tuple[str, str]:
    """A permission request -> (what to show, what to say)."""
    kind, title = tool_call.get("kind"), tool_call.get("title") or "a tool"
    raw = tool_call.get("rawInput") if isinstance(tool_call.get("rawInput"), dict) else {}
    target = acp_target(tool_call)
    name = os.path.basename(target) if target else title
    if kind == "execute":
        return f"Run: {raw.get('command') or title}", "Can I run a command?"
    if kind in ("edit", "delete", "move"):
        verb = {"edit": "Edit", "delete": "Delete", "move": "Move"}[kind]
        return f"{verb} {target or title}", f"Can I {verb.lower()} {name}?"
    if kind == "fetch":
        return f"Fetch {raw.get('url') or title}", "Can I open a web page?"
    return f"Use {title}", f"Can I use {title.replace('_', ' ')}?"


def acp_output(update: dict) -> str:
    """A finished tool call's result as text."""
    parts = []
    for item in update.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "content" and isinstance(item.get("content"), dict):
            if item["content"].get("type") == "text":
                parts.append(str(item["content"].get("text", "")))
        elif item.get("type") == "diff":
            parts.append(f"{item.get('path', '')}:\n{item.get('newText', '')}")
        elif item.get("type") == "terminal":
            parts.append("(ran in a terminal)")
    if not parts:
        raw = update.get("rawOutput")
        if isinstance(raw, dict) and isinstance(raw.get("output"), str):
            parts.append(raw["output"])
        elif raw not in (None, {}, ""):
            parts.append(raw if isinstance(raw, str) else json.dumps(raw, indent=2, default=str))
    return "\n".join(p for p in parts if p)


# --- the backend -----------------------------------------------------------------------------------------------------
class AcpBackend(TurnReporter, Backend):
    """Any ACP agent. A subclass says how to start it (``_command``, ``_env``) and what its permission modes are."""

    capabilities = frozenset({"permission_prompts", "resume", "interrupt", "skip_permissions", "context"})

    def __init__(self, host):
        super().__init__(host)
        self._init_turns()
        self.client: AcpClient | None = None
        self._sid: str | None = None
        self._applied: tuple | None = None
        self._replaying = False                 # session/load replays the old conversation: not for the user's ears
        self._commands: set[str] = set()        # the slash commands the agent offers ("compact", ...)
        self._got = False
        self._outcome: bool | None = None
        self._fail_reason: str | None = None
        self._result = asyncio.Event()
        self._cost = 0.0                        # the session's cost so far, as the agent reports it
        self._cost0 = 0.0

    @property
    def cfg(self) -> dict:
        return self.host.backend_cfg

    # --- how to start the agent (subclasses) ---
    def _command(self) -> list[str]:
        raise NotImplementedError

    def _env(self) -> dict:
        return dict(os.environ)

    @classmethod
    def validate(cls, cfg: dict) -> None:
        super().validate(cfg)
        options = cfg.get("options", {})
        cli = options.get("cli_path")
        if cli is not None and not (isinstance(cli, str) and Path(cli).is_file()):
            raise ValueError(f"options.cli_path: not a file: {cli!r}")
        args = options.get("cli_args")
        if args is not None and not (isinstance(args, list) and all(isinstance(a, str) for a in args)):
            raise ValueError("options.cli_args: expected a list of strings")

    def _snapshot(self) -> tuple:
        c = self.cfg
        return c["cwd"], c.get("model") or None, bool(c["skip_permissions"]), c["permission_mode"], bool(self._mcp_servers())

    def _mcp_servers(self) -> list[dict]:
        """MCP servers Wireface adds to the agent's own: the phone tools, while the phone line is on."""
        phone = getattr(self.host, "phone", None)
        if phone is None or not self.host.cfg.get("phone", {}).get("enabled"):
            return []
        spec = phone.stdio_server()
        return [{"name": "phone", "command": spec["command"], "args": spec["args"],
                 "env": [{"name": k, "value": v} for k, v in spec["env"].items()]}]

    # --- session ---
    async def _connect(self):
        if self.client:
            return
        self.connecting = True
        self.host.changed()
        client = None
        applied = self._snapshot()              # what this process starts with: the settings can change meanwhile
        try:
            if ensure_instructions(self.cfg["cwd"]):
                self.note("Created AGENTS.md", f"{self.cfg['cwd']}\nso replies are written to be spoken")
            argv = self._command()
            client = AcpClient(argv, self.cfg["cwd"], self._env(), self._on_notify, self._on_request, self._on_exit)
            await client.start()
            init = await client.call("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}}, timeout=60)
            can_load = bool((init.get("agentCapabilities") or {}).get("loadSession"))
            sid = self.host.state.get("session_id")
            resumed = False
            if sid and can_load:
                self._replaying = True
                try:
                    await client.call("session/load", {"sessionId": sid, "cwd": self.cfg["cwd"],
                                                       "mcpServers": self._mcp_servers()}, timeout=60)
                    resumed = True
                except AcpError as e:
                    log.warning("could not resume session %s (%s); starting a new one", sid, e)
                finally:
                    self._replaying = False
            if not resumed:
                sid = (await client.call("session/new", {"cwd": self.cfg["cwd"], "mcpServers": self._mcp_servers()},
                                         timeout=60))["sessionId"]
        except Exception as e:
            if client:
                await client.close()
            self.last_error = f"{self.label} could not start: {e}"
            self.log.emit("error", summary=self.last_error)
            raise
        finally:
            self.connecting = False
            self.host.changed()
        self.client, self._sid, self.connected, self.last_error = client, sid, True, None
        self._resumed = resumed
        self._applied = applied
        if sid != self.host.state.get("session_id"):
            self.host.state["session_id"] = sid
            self.host.save_state()
        info = (init.get("agentInfo") or {})
        log.info("%s session started in %s (%s)", self.label, self.cfg["cwd"], "resumed" if resumed else "new")
        self.note("Session started" + (" (permissions skipped)" if self.cfg["skip_permissions"] else ""),
                  f"{self.cfg['cwd']}\n{info.get('name', self.label)} {info.get('version', '')}\n"
                  f"{'resumed' if resumed else 'new conversation'}")
        self.host.changed()
        await self.apply_config()                # anything changed while it started: start again with it

    async def _close(self):
        client, self.client = self.client, None
        self.connected, self._resumed = False, False
        if client:
            await client.close()
        self._abort_turn("the session was closed")
        self.tasks.clear()
        self.host.changed()

    async def close(self):
        await self._close()

    async def _autostart(self):
        if not self.cfg.get("auto_start") or self.client:
            return
        try:
            await self._connect()
        except Exception as e:
            log.exception("could not start %s", self.label)
            self.host.notify(f"{self.label} could not start: {e}")

    async def run(self):
        await self._start()
        while True:
            kind, text = await self.inbox.get()
            if kind == "reset":
                await self._close()
                self.host.state["session_id"] = None
                self.host.save_state()
                await self._start()
            elif kind == "reconnect":
                await self._start(restart=True)
            elif kind == "quit":
                await self._close()
                return
            elif kind == "compact":
                await self._compact_now("asked")
            elif kind == "user":
                self.active = True
                self.host.changed()
                try:
                    await self._ask(text)
                except Exception as e:
                    log.exception("turn failed")
                    self.last_error = f"{self.label} stopped: {e}"
                    self.host.notify(self.last_error)
                    self.problem(self.last_error)
                    await self._close()
                    continue
                finally:
                    self.active, self.op = False, None
                    self.host.changed()
                await self._maybe_compact()

    async def halt(self):
        self._drain()
        if self.client and self.active and self._sid:
            self.client.notify("session/cancel", {"sessionId": self._sid})

    async def apply_config(self):
        if not self.client:
            if self.cfg.get("auto_start") and not self.connecting:
                self.reconnect()
        elif self._snapshot() != self._applied:
            self.reconnect()                     # these only apply to a new agent process; the session resumes

    # --- one turn ---
    async def _attempt(self, text: str, prompt: str | None = None) -> None:
        try:
            await self._turn(text, prompt)
        except AcpError as e:
            if self._got:
                raise
            raise NoReply(str(e)) from e

    async def _compact_session(self) -> None:
        """The agent's own /compact command (OpenCode: "Compact the session")."""
        await self._connect()
        if "compact" not in self._commands:
            raise AcpError(f"{self.label} has no compact command")
        reply = await self.client.call("session/prompt", {"sessionId": self._sid,
                                                          "prompt": [{"type": "text", "text": "/compact"}]})
        if reply.get("stopReason") not in (None, "end_turn"):
            raise AcpError(f"it stopped ({reply.get('stopReason')})")
        self.context = None                      # known again with the next prompt

    async def _turn(self, text: str, prompt: str | None = None) -> None:
        await self._connect()
        self.active = True
        self._got, self._outcome, self._fail_reason = False, None, None
        self._result = asyncio.Event()
        self._cost0 = self._cost
        self.begin_turn(text, "user")
        started = time.monotonic()
        failed, interrupted, usage = False, False, {}
        try:
            reply = await self.client.call("session/prompt", {"sessionId": self._sid,
                                                              "prompt": [{"type": "text", "text": prompt or text}]})
            self._resumed = False
            stop = reply.get("stopReason")
            interrupted = stop == "cancelled"
            failed = stop in ("refusal",)
            usage = reply.get("usage") or {}
            if stop == "max_tokens":
                self.host.notify("The reply was cut off: the agent ran out of tokens.")
        except AcpError as e:
            failed = True
            if not self._got:
                raise                            # nothing came back at all: _ask decides what to do next
            self.problem(str(e))
            self.host.notify(str(e)[:300])
        finally:
            self.end_turn(failed=failed, interrupted=interrupted, duration_ms=round((time.monotonic() - started) * 1000),
                          tokens=usage.get("totalTokens") or ((usage.get("inputTokens") or 0) + (usage.get("outputTokens") or 0)),
                          cost_usd=round(self._cost - self._cost0, 6) if self._cost > self._cost0 else None,
                          session_cost_usd=round(self._cost, 6) if self._cost else None)
            self.session_cost = self._cost or None
            self.active, self.op = False, None

    def _wake(self):
        """The agent started working on its own."""
        if not self.active:
            self.active = True
            self._got = False
            self.begin_turn("", "background")
            self.host.changed()

    def _abort_turn(self, reason: str):
        if self.active or self._turn_id:
            self._fail_reason = self._fail_reason or reason
            self.end_turn(failed=True, stopped=reason)
            self.active, self.op = False, None

    def _on_exit(self, reason: str):
        """The agent process went away on its own."""
        client, self.client = self.client, None
        if client is None:
            return
        self.connected = False
        self.last_error = f"{self.label} stopped: {reason}"
        self.tasks.clear()
        self.problem(self.last_error)
        self._abort_turn(reason)
        self.host.notify(self.last_error)
        self.host.changed()

    # --- what the agent says (notifications) ---
    def _on_notify(self, method: str, params: dict):
        if method != "session/update":
            return
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "available_commands_update":  # sent as the session opens: before we know its id, or during replay
            self._u_available_commands_update(update)
            return
        if self._replaying or params.get("sessionId") != self._sid:
            return
        handler = getattr(self, f"_u_{kind}", None)
        if handler:
            handler(update)

    def _u_agent_message_chunk(self, u: dict):
        content = u.get("content") or {}
        if content.get("type") != "text" or not content.get("text"):
            return
        self._wake()
        self._got = True
        self.end_thinking()
        self._set_op(None)
        self.say_delta(content["text"], u.get("messageId"))

    def _u_agent_thought_chunk(self, u: dict):
        content = u.get("content") or {}
        if content.get("type") == "text" and content.get("text"):
            self._wake()
            self.think_delta(content["text"])

    def _u_tool_call(self, u: dict):
        self._wake()
        self._got = True
        target = acp_target(u)
        title = u.get("title") or "tool"
        self.tool_start(u["toolCallId"], title, target, u.get("rawInput"), kind=u.get("kind"))
        self._set_op(acp_op(title, u.get("kind"), target))

    def _u_tool_call_update(self, u: dict):
        tid = u.get("toolCallId")
        if not tid:
            return
        if self.log.get(tid) is None:
            self._u_tool_call({**u, "toolCallId": tid})
        target = acp_target(u)
        self.tool_update(tid, summary=target, input=u.get("rawInput"), **({"name": u["title"]} if u.get("title") else {}))
        status = u.get("status")
        if status in ("completed", "failed"):
            self.tool_end(tid, status == "completed", acp_output(u))
        elif status == "in_progress" and target:
            self._set_op(acp_op(u.get("title") or "tool", u.get("kind") or (self.log.get(tid).extra or {}).get("kind"), target))

    def _u_plan(self, u: dict):
        entries = u.get("entries") or []
        lines = [f"[{'x' if e.get('status') == 'completed' else ' '}] {e.get('content', '')}" for e in entries if isinstance(e, dict)]
        if lines and self._turn_id:
            self.note("Plan", "\n".join(lines), id=f"plan:{self._turn_id}")

    def _u_usage_update(self, u: dict):
        amount = (u.get("cost") or {}).get("amount")
        if isinstance(amount, (int, float)):
            self._cost = float(amount)
        used, size = u.get("used"), u.get("size")
        if isinstance(used, int) and isinstance(size, int) and size > 0:
            self.context = {"used": used, "size": size}
            self.host.changed()

    def _u_available_commands_update(self, u: dict):
        self._commands = {str(c.get("name")) for c in u.get("availableCommands") or [] if isinstance(c, dict)}

    def _u_session_info_update(self, u: dict):
        compaction = (u.get("_meta") or {}).get("opencode/compaction")
        if isinstance(compaction, dict):
            self._on_compaction(compaction)
        retry = (u.get("_meta") or {}).get("opencode/retry")
        if retry:
            err = retry.get("error") or {}
            body = (err.get("response") or {}).get("body") or ""
            try:
                detail = json.loads(body).get("error", {}).get("metadata", {}).get("raw") or err.get("message")
            except (ValueError, AttributeError):
                detail = err.get("message")
            self._set_op(f"waiting for the model (try {retry.get('attempt', '?')})")
            self.note(f"The model provider is slow or limiting requests: trying again (attempt {retry.get('attempt', '?')})",
                      str(detail or ""), id=f"retry:{self._turn_id or 'idle'}")
        elif retry is None and "opencode/retry" in (u.get("_meta") or {}):
            self._set_op(None)

    def _on_compaction(self, c: dict):
        """OpenCode compacting the session: on request (ours), or on its own as the context fills up."""
        status = c.get("status")
        if status == "started":
            self._set_op(COMPACTING)
            return
        if self.op == COMPACTING:
            self._set_op(None)
        if status == "completed":
            self.context = None
            self.host.changed()
            if c.get("reason") != "manual":          # ours is reported by _compact_now
                self.note("Compacted the conversation", f"{self.label} did it on its own ({c.get('reason') or 'auto'}).")
        elif status:
            self.problem(f"{self.label} couldn't compact the conversation ({status})")

    def _set_op(self, op: str | None):
        if op != self.op:
            self.op = op
            self.host.changed()

    # --- what the agent asks (requests) ---
    async def _on_request(self, method: str, params: dict):
        if method == "session/request_permission":
            return await self._permission(params)
        raise AcpError(f"{method} is not supported", -32601)

    async def _permission(self, params: dict) -> dict:
        tool_call = params.get("toolCall") or {}
        options = [o for o in params.get("options") or [] if isinstance(o, dict) and o.get("optionId")]

        def pick(kind: str):
            return next((o["optionId"] for o in options if o.get("kind") == kind), None)

        show, say = acp_request_text(tool_call)
        ev = self.log.emit("permission", turn_id=self._turn_id, name=tool_call.get("kind") or "tool", summary=show, status="running")
        if self.cfg["skip_permissions"]:
            choice = pick("allow_once") or pick("allow_always") or (options[0]["optionId"] if options else None)
            self.log.update(ev.id, status="ok", output="allowed (permissions skipped)")
        else:
            allow, always = await self.host.ask_permission(show, say, bool(pick("allow_always")))
            if allow:
                choice = (pick("allow_always") if always else None) or pick("allow_once") or pick("allow_always")
            else:
                choice = pick("reject_once") or pick("reject_always")
            self.log.update(ev.id, status="ok" if allow else "error",
                            output=("allowed" + (" for the session" if always else "")) if allow else "denied")
        if choice is None:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": choice}}


class OpenCodeBackend(AcpBackend):
    id = "opencode"
    label = "OpenCode"
    capabilities = AcpBackend.capabilities | {"compact"}
    permission_modes = {"default": "Ask before changes", "own": "Use OpenCode's own settings"}
    permission_help = "Ask: OpenCode asks before it edits files or runs commands. Its own settings: whatever your OpenCode config says."

    def _command(self) -> list[str]:
        options = self.cfg.get("options", {})
        exe = find_executable("opencode", options.get("cli_path"),
                              shim_globs=["node_modules/@opencode/cli/bin/opencode.exe"],
                              fallbacks=[Path.home() / ".opencode" / "bin" / "opencode.exe"])
        return [exe, *(options.get("cli_args") or ["acp"])]

    def _env(self) -> dict:
        """OpenCode takes extra configuration as JSON in an environment variable: the model, and (unless it should
        use its own settings) asking before it edits files or runs commands."""
        env = dict(os.environ)
        try:
            inline = json.loads(env.get("OPENCODE_CONFIG_CONTENT") or "{}")
        except ValueError:
            inline = {}
        cfg = self.cfg
        if cfg.get("model"):
            inline["model"] = cfg["model"]
        if cfg["permission_mode"] == "default" and not cfg["skip_permissions"]:
            inline["permission"] = {**(inline.get("permission") or {}), "edit": "ask", "bash": "ask"}
        if inline:
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps(inline)
        return env
