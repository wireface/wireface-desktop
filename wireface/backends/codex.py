"""OpenAI Codex as the face's agent, driven through ``codex exec --json``.

Codex has no always-on session to attach to this way: each prompt is one ``codex exec`` process (``codex exec
resume <thread>`` for the later ones, so the conversation continues), and it reports what it does as a stream of
JSON lines. That also means it cannot stop to ask permission: what it may do is decided up front by its sandbox
(``backend.permission_mode``), or lifted altogether by ``backend.skip_permissions``
(``--dangerously-bypass-approvals-and-sandbox``).

Events handled (one JSON object per line): thread.started, turn.started, item.started / item.completed with item
types agent_message, reasoning, command_execution, file_change, mcp_tool_call, web_search, todo_list and error,
turn.completed (token usage), turn.failed, error.

Codex compacts the conversation on its own as it nears the model's limit (``model_auto_compact_token_limit``), but
``exec`` can't be asked to, so compacting on request goes through ``codex app-server`` (``thread/compact/start``)
for the saved thread, which ``exec resume`` then carries on from.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import re
import time
from pathlib import Path

from .base import Backend, NoReply, ensure_instructions
from .acp import AcpClient, AcpError
from .exe import CREATE_NO_WINDOW, AgentNotFound, find_executable, kill_tree
from .turns import TurnReporter

log = logging.getLogger(__name__)

POWERSHELL = re.compile(r'^"?[^\s"]*powershell(?:\.exe)?"?\s+(?:-\w+\s+)*-Command\s+(.*)$', re.I | re.S)


def command_summary(command: str) -> str:
    """A shell command for the one-line list: without Codex's PowerShell wrapper, first line only."""
    m = POWERSHELL.match(command.strip())
    inner = (m.group(1) if m else command).strip().strip('"')
    return (inner.splitlines() or [""])[0][:200]


def change_summary(changes: list) -> str:
    parts = []
    for c in changes or []:
        if isinstance(c, dict) and c.get("path"):
            parts.append(f"{os.path.basename(str(c['path']))} ({c.get('kind', 'change')})")
    return ", ".join(parts)[:200]


class CodexBackend(TurnReporter, Backend):
    id = "codex"
    label = "Codex"
    capabilities = frozenset({"resume", "interrupt", "skip_permissions", "compact"})
    permission_modes = {"default": "Edit the working folder", "read-only": "Read only"}
    permission_help = ("Codex cannot ask first when run this way, so this decides what it is allowed to do: edit files and "
                       "run commands in the working folder, or only look.")

    def __init__(self, host):
        super().__init__(host)
        self._init_turns()
        self.proc: asyncio.subprocess.Process | None = None
        self._halted = False
        self._got = False
        self._failure: str | None = None
        self._usage: dict = {}
        self._warned: set[str] = set()           # warnings already shown (Codex repeats them on every run)

    @property
    def cfg(self) -> dict:
        return self.host.backend_cfg

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

    # --- how Codex is run ---
    def _phone_args(self) -> list[str]:
        """The phone tools (phone/mcp.py) as an MCP server for this run, while the phone line is on."""
        phone = getattr(self.host, "phone", None)
        if phone is None or not self.host.cfg.get("phone", {}).get("enabled"):
            return []
        spec = phone.stdio_server()
        lit = lambda v: "'" + str(v).replace("'", "") + "'"              # a TOML literal string: no escapes needed
        env = ", ".join(f"{k} = {lit(v)}" for k, v in spec["env"].items())
        return ["-c", f"mcp_servers.phone.command={lit(spec['command'])}",
                "-c", "mcp_servers.phone.args=[" + ", ".join(lit(a) for a in spec["args"]) + "]",
                "-c", f"mcp_servers.phone.env={{{env}}}"]

    def _exe(self) -> str:
        return find_executable("codex", self.cfg.get("options", {}).get("cli_path"),
                               shim_globs=["node_modules/@openai/codex/node_modules/@openai/codex-*/vendor/*/bin/codex.exe"])

    def _command(self, resume: str | None) -> list[str]:
        cfg, options = self.cfg, self.cfg.get("options", {})
        exe = self._exe()
        argv = [exe, "exec"] + (["resume"] if resume else []) + ["--json", "--skip-git-repo-check"]
        if cfg.get("model"):
            argv += ["-m", cfg["model"]]
        if cfg["skip_permissions"]:
            argv += ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            argv += ["-c", f'sandbox_mode="{"read-only" if cfg["permission_mode"] == "read-only" else "workspace-write"}"']
        argv += self._phone_args()
        argv += list(options.get("cli_args") or [])
        if resume:
            argv += [resume]
        return argv + ["-"]                      # the prompt comes on stdin

    def _app_server_command(self) -> list[str]:
        return [self._exe(), "app-server"]          # (options.cli_args are for exec, and may not fit app-server)

    # --- session: there is no process between turns, so "connected" means "ready" ---
    async def _connect(self):
        if self.connected:
            return
        try:
            self._command(None)                   # is Codex installed?
            if ensure_instructions(self.cfg["cwd"]):
                self.note("Created AGENTS.md", f"{self.cfg['cwd']}\nso replies are written to be spoken")
        except AgentNotFound as e:
            self.last_error = str(e)
            self.log.emit("error", summary=self.last_error)
            raise
        self.connected, self.last_error = True, None
        self._resumed = bool(self.host.state.get("session_id"))
        self.note("Ready" + (" (permissions skipped)" if self.cfg["skip_permissions"] else ""),
                  f"{self.cfg['cwd']}\n{'resumes the last conversation' if self.host.state.get('session_id') else 'new conversation'}")
        self.host.changed()

    async def _autostart(self):
        if not self.cfg.get("auto_start") or self.connected:
            return
        try:
            await self._connect()
        except Exception as e:
            self.host.notify(f"{self.label} could not start: {e}")

    async def _close(self):
        await self._kill()
        self.connected = False
        self.tasks.clear()
        self.host.changed()

    async def close(self):
        await self._close()

    async def _kill(self):
        proc = self.proc
        if proc and proc.returncode is None:
            kill_tree(proc.pid)
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                pass

    async def run(self):
        await self._start()
        while True:
            kind, text = await self.inbox.get()
            if kind == "reset":
                self.host.state["session_id"] = None
                self.host.save_state()
                self.connected = False
                await self._start()
            elif kind == "reconnect":
                self._reconnect_queued = self.connected = False     # each turn is a new process: nothing to close
                await self._start()
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
                    self.connected = False
                finally:
                    self.active, self.op = False, None
                    self.host.changed()

    async def halt(self):
        self._drain()
        if self.proc and self.proc.returncode is None and self.active:
            self._halted = True
            kill_tree(self.proc.pid)

    async def apply_config(self):
        """Everything applies from the next prompt on (the command is built per turn); a new folder gets its AGENTS.md."""
        if self.connected:
            if ensure_instructions(self.cfg["cwd"]):
                self.note("Created AGENTS.md", self.cfg["cwd"])
        elif self.cfg.get("auto_start"):
            self.reconnect()

    # --- one turn ---
    async def _attempt(self, text: str, prompt: str | None = None) -> None:
        await self._turn(text, self.host.state.get("session_id"), prompt)

    async def _turn(self, text: str, resume: str | None, prompt: str | None = None):
        await self._connect()
        self.active = True
        self._got, self._halted, self._failure, self._usage = False, False, None, {}
        self.begin_turn(text, "user")
        started = time.monotonic()
        stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        proc = None
        try:
            proc = self.proc = await asyncio.create_subprocess_exec(
                *self._command(resume), cwd=self.cfg["cwd"], env=dict(os.environ), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=32 * 1024 * 1024,
                creationflags=CREATE_NO_WINDOW)

            async def drain_stderr():
                async for line in proc.stderr:
                    text_ = line.decode("utf-8", "replace").rstrip()
                    if text_ and "Reading additional input from stdin" not in text_:
                        stderr_tail.append(text_)

            err_task = asyncio.create_task(drain_stderr())
            proc.stdin.write((prompt or text).encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("{"):
                    continue
                try:
                    self._event(json.loads(line))
                except ValueError:
                    continue
                except Exception:
                    log.exception("handling a Codex event")
            code = await proc.wait()
            await err_task
        except BaseException:
            if proc and proc.returncode is None:
                kill_tree(proc.pid)
            self.end_turn(failed=True, stopped="the turn was cancelled")
            raise
        finally:
            self.proc = None
        failed = bool(self._failure) or (code != 0 and not self._halted)
        if not failed:
            self._resumed = False
        usage = self._usage
        self.end_turn(failed=failed and not self._halted, interrupted=self._halted,
                      duration_ms=round((time.monotonic() - started) * 1000),
                      tokens=(usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0))
        if failed and not self._halted:
            reason = self._failure or (stderr_tail[-1] if stderr_tail else f"Codex exited with code {code}")
            if not self._got:
                raise NoReply(reason)
            self.host.notify(reason[:300])

    async def _compact_session(self) -> None:
        """Compact the saved thread through ``codex app-server``: resume it, ask for ``thread/compact/start``, and wait
        for that turn to finish. The next ``exec resume`` carries on from the summary."""
        tid = self.host.state.get("session_id")
        done = asyncio.get_running_loop().create_future()

        def on_notify(method: str, params: dict):
            if params.get("threadId") != tid or done.done():
                return
            if method == "turn/completed":
                turn = params.get("turn") or {}
                if turn.get("status") == "completed":
                    done.set_result(None)
                else:
                    done.set_exception(AcpError(str((turn.get("error") or {}).get("message") or turn.get("status"))))
            elif method == "error" and not params.get("willRetry"):
                done.set_exception(AcpError(str((params.get("error") or {}).get("message") or "error")))

        async def on_request(method: str, params: dict):
            raise AcpError(f"{method} is not expected while compacting", -32601)

        def on_exit(reason: str):
            if not done.done():
                done.set_exception(AcpError(reason))

        client = AcpClient(self._app_server_command(), self.cfg["cwd"], dict(os.environ), on_notify, on_request, on_exit)
        await client.start()
        try:
            await client.call("initialize", {"clientInfo": {"name": "wireface", "title": "Wireface", "version": "1"}},
                              timeout=60)
            client.notify("initialized", {})
            resume = {"threadId": tid, "cwd": self.cfg["cwd"], "excludeTurns": True}
            if self.cfg.get("model"):
                resume["model"] = self.cfg["model"]
            await client.call("thread/resume", resume, timeout=120)
            await client.call("thread/compact/start", {"threadId": tid}, timeout=60)
            await asyncio.wait_for(done, 600)
        finally:
            await client.close()

    # --- the event stream ---
    def _event(self, ev: dict):
        kind = ev.get("type")
        if kind == "thread.started":
            tid = ev.get("thread_id")
            if tid and tid != self.host.state.get("session_id"):
                self.host.state["session_id"] = tid
                self.host.save_state()
        elif kind in ("item.started", "item.updated", "item.completed"):
            self._item(kind.split(".")[1], ev.get("item") or {})
        elif kind == "turn.completed":
            self._usage = ev.get("usage") or {}
            self.last_error = None
        elif kind == "turn.failed":
            self._failure = str((ev.get("error") or {}).get("message") or "the turn failed")
            self.problem(self._failure)
        elif kind == "error":
            message = str(ev.get("message") or "error")
            if message not in self._warned:
                self._warned.add(message)
                self.problem(message)
            self._failure = self._failure or message

    def _item(self, phase: str, item: dict):
        kind, iid = item.get("type"), str(item.get("id") or "")
        done = phase == "completed"
        if kind == "agent_message":
            if done:
                self._got = True
                self._set_op(None)
                self.say_whole(str(item.get("text") or ""))
        elif kind == "reasoning":
            if done and item.get("text"):
                self.log.emit("thinking", turn_id=self._turn_id, output=str(item["text"]))
        elif kind == "command_execution":
            command = str(item.get("command") or "")
            if phase == "started":
                self._got = True
                self.tool_start(iid, "shell", command_summary(command), command)
                self._set_op("running a command")
            elif done:
                ok = item.get("status") == "completed" and item.get("exit_code") in (0, None)
                self.tool_end(iid, ok, item.get("aggregated_output") or (None if ok else f"exit code {item.get('exit_code')}"),
                              extra={"exit_code": item.get("exit_code")})
        elif kind == "file_change":
            changes = item.get("changes") or []
            if phase == "started":
                self._got = True
                self.tool_start(iid, "apply_patch", change_summary(changes), changes)
                self._set_op("editing " + (os.path.basename(str(changes[0].get("path", ""))) if changes and isinstance(changes[0], dict) else "files"))
            elif done:
                self.tool_end(iid, item.get("status") == "completed", change_summary(changes))
        elif kind == "mcp_tool_call":
            name = f"{item.get('server', 'mcp')}.{item.get('tool', 'tool')}"
            if phase == "started":
                self._got = True
                self.tool_start(iid, name, "", item.get("arguments"))
                self._set_op(f"using {item.get('tool', 'a tool')}")
            elif done:
                err = item.get("error")
                self.tool_end(iid, item.get("status") == "completed" and not err, err or item.get("result"))
        elif kind == "web_search":
            if phase == "started":
                self._got = True
                self.tool_start(iid, "web_search", str(item.get("query") or ""), None)
                self._set_op("looking online")
            elif done:
                self.tool_end(iid, True)
        elif kind == "todo_list":
            lines = [f"[{'x' if i.get('completed') else ' '}] {i.get('text', '')}" for i in item.get("items") or [] if isinstance(i, dict)]
            if lines:
                self.note("Plan", "\n".join(lines), id=f"plan:{iid}")
        elif kind == "error":
            message = str(item.get("message") or "")
            if done and message and message not in self._warned:        # a warning about its own config, say
                self._warned.add(message)
                self.note(message.splitlines()[0][:200], message)
        else:
            log.debug("unhandled Codex item: %s", kind)

    def _set_op(self, op: str | None):
        if op != self.op:
            self.op = op
            self.host.changed()
