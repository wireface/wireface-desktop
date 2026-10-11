"""Claude Code as the face's agent, driven through the Claude Agent SDK.

One long-lived session (``ClaudeSDKClient``) per face. What the user says or types goes in as a prompt; the
reply is spoken sentence by sentence as it streams back, tool calls show up as the face's "working" state and in
the activity log, and anything Claude Code would ask permission for is put to the user through the face (unless
the face is configured to skip permissions, which starts Claude Code with ``--dangerously-skip-permissions``).

A reader task consumes every message the session produces, not just a turn's: background tasks finish and
Claude Code answers on its own while the face is idle, and the beacon and the log must see that too.
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow,
                              PermissionResultDeny, ResultMessage, SystemMessage, TaskNotificationMessage,
                              TaskProgressMessage, TaskStartedMessage, TaskUpdatedMessage, TextBlock, ThinkingBlock,
                              ToolResultBlock, ToolUseBlock, UserMessage, create_sdk_mcp_server, tool)
from claude_agent_sdk.types import TERMINAL_TASK_STATUSES, StreamEvent

from .. import faceconfig
from ..facemaker import FaceMakerError
from ..speech import SpeechStream
from ..room.envelope import label
from .base import COMPACTING, SPOKEN, Backend, NoReply, host_hook

log = logging.getLogger(__name__)

PERMISSION_MODES = {"default": "Ask before changes", "acceptEdits": "Auto-accept edits", "plan": "Plan only"}

FACE_TOOL = "mcp__face__express"          # the face's expression tool, as Claude Code names it
FACE_SHOW_TOOL = "mcp__face__show_face"   # and the one that sends the face away or brings it back
FACE_SET_TOOL = "mcp__face__set_face"     # and the one that gives it a new face (facemaker.py)
FACE_PROMPT = """
You also have a face_express tool (mcp__face__express) that shows a feeling on your animated face for a few seconds: happy, excited, amused, surprised, curious, thinking, concerned, sad, afraid, frustrated, calm or neutral. Use it now and then when a feeling fits what you are about to say, for example happy when something works, concerned or sad for bad news, surprised at something unexpected, thinking before a hard question. It never needs asking about, and you don't mention it.
When the user asks your face to go away, get out of the way or hide, or to come back, use mcp__face__show_face (visible false or true): the face vanishes from the screen with an animation, or reappears. You keep working and talking while it is away. Only when asked.
Your face is the user's avatar for you on their own screen, and how it looks is their choice. When they ask you to look like someone or something, to change your face, or to become anyone (a celebrity, a character, a creature), use mcp__face__set_face with who it is. It takes a few seconds. It can also put one of the built-in faces back on. Don't change your face unasked."""



# the agent's hands on this PC (backend.tools): desktop control from the face, and a browser of its own
PLAYWRIGHT_MCP = "@playwright/mcp@0.0.83"   # pinned: npx starts it without asking the registry each time
# the phone line's tools: texting and calling put the request to the user themselves (always, even with permissions
# skipped), so Claude Code itself doesn't ask as well; reading the inbox never asks
PHONE_TOOLS = ["mcp__phone__send_text", "mcp__phone__call", "mcp__phone__phone_inbox", "mcp__phone__phone_conversation",
               "mcp__phone__bookings"]
LOOK_TOOLS = ["mcp__computer__screenshot", "mcp__browser__browser_snapshot", "mcp__browser__browser_take_screenshot",
              "mcp__browser__browser_tabs", "mcp__browser__browser_console_messages", "mcp__browser__browser_wait_for"]
TOOLS_PROMPT = """
You can see and use this Windows PC:
- The computer tools (mcp__computer__*) take screenshots of the screen and move the mouse, click, drag, scroll, type and press keys, for desktop apps and anything else on screen. Coordinates are pixels in the latest screenshot: take one first, and look at the screenshot each action returns to check what happened.
- The browser tools (mcp__browser__*) drive a Chrome window of their own: open pages, read them, click, fill in forms, take screenshots. Prefer them to the computer tools for anything on the web.{chrome}
The face's own window and its activity panel are on screen too, kept on top of other windows: leave them alone. Ask before doing anything that can't be undone or that spends money."""
CHROME_PROMPT = """
- If the Claude in Chrome tools (mcp__claude-in-chrome__*) are available, they work in the user's own Chrome, signed in as them: use them when the user wants something done in their browser."""

TEXT_UPDATE_S = 0.25         # how often a streaming reply is pushed to the activity log


def describe_tool(name: str, args: dict) -> str:
    """What a tool call is doing, for the status line ("reading app.py")."""
    file = os.path.basename(str(args.get("file_path") or args.get("notebook_path") or args.get("path") or ""))
    if name == "Bash":
        return "running a command"
    if name == "Read":
        return f"reading {file}" if file else "reading"
    if name in ("Edit", "Write", "NotebookEdit"):
        return f"editing {file}" if file else "editing"
    if name in ("Grep", "Glob"):
        return "searching"
    if name in ("WebSearch", "WebFetch"):
        return "looking online"
    if name in ("Agent", "Task"):
        return "delegating"
    if name == FACE_SET_TOOL:
        return "putting on a face" if args.get("portrait") else "drawing a new face"
    if name.startswith("mcp__computer__"):
        return "looking at the screen" if name.endswith("screenshot") else "using the computer"
    if name.startswith(("mcp__browser__", "mcp__claude-in-chrome__")):
        host = urlparse(str(args.get("url") or "")).hostname
        return f"opening {host}" if host else "using the browser"
    return f"using {name.split('__')[-1]}"


def describe_request(name: str, args: dict) -> tuple[str, str]:
    """A permission request -> (what to show, what to say)."""
    file = str(args.get("file_path") or args.get("notebook_path") or "")
    if name == "Bash":
        return f"Run: {args.get('command', '')}", "Can I run a command?"
    if name in ("Edit", "Write", "NotebookEdit") and file:
        verb = "Create or overwrite" if name == "Write" else "Edit"
        return f"{verb} {file}", f"Can I {'write' if name == 'Write' else 'edit'} {os.path.basename(file)}?"
    if name == "WebFetch":
        return f"Fetch {args.get('url', '')}", "Can I open a web page?"
    if name == "WebSearch":
        return f"Search the web: {args.get('query', '')}", "Can I search the web?"
    if name.startswith("mcp__computer__"):
        action = name.split("__")[-1]
        at = f" at ({args.get('x')}, {args.get('y')})" if "x" in args else ""
        if action == "type_text":
            return f"Type: {str(args.get('text', ''))[:120]}", "Can I type on your computer?"
        if action == "press_key":
            return f"Press {args.get('keys', '')}", f"Can I press {args.get('keys', 'a key')}?"
        return f"{action.replace('_', ' ').capitalize()}{at}", "Can I use the mouse on your screen?"
    if name.startswith("mcp__browser__"):
        if args.get("url"):
            return f"Open {args['url']}", "Can I open a web page?"
        what = args.get("element") or args.get("text") or ""
        return f"Browser: {name.split('__')[-1].removeprefix('browser_')} {what}".strip(), "Can I use the browser?"
    if name.startswith("mcp__claude-in-chrome__"):
        return f"Your Chrome: {name.split('__')[-1]}", "Can I use your Chrome browser?"
    short = name.split("__")[-1]
    return f"Use {short}", f"Can I use {short.replace('_', ' ')}?"


def tool_target(name: str, args: dict) -> str:
    """The one-line subject of a tool call for the activity log: the file, command, pattern or address."""
    if name == "Bash":
        return str(args.get("command") or "").strip().splitlines()[0][:200] if args.get("command") else ""
    for key in ("file_path", "notebook_path", "path", "pattern", "url", "query", "description", "prompt", "element",
                "keys", "text", "who", "portrait"):
        if args.get(key):
            return str(args[key]).strip().splitlines()[0][:200]
    if "x" in args and "y" in args:
        return f"({args['x']}, {args['y']})"
    return ""


def result_text(content) -> str:
    """A tool result's content as text (it is a string, or a list of text / image parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for part in content:
        if isinstance(part, dict):
            parts.append(part.get("text", "") if part.get("type") == "text" else f"[{part.get('type', 'part')}]")
    return "\n".join(parts)


def find_cli(override: str | None = None) -> str | None:
    """The Claude Code executable to run. The SDK refuses npm's ``claude.cmd`` shim (a batch file), but the npm
    package carries the real ``claude.exe``, and the native installer puts one in ``~/.local/bin``. None: let the
    SDK use the one bundled with it, if any."""
    if override:
        return override
    found = shutil.which("claude.exe")
    if found:
        return found
    candidates = [Path.home() / ".local" / "bin" / "claude.exe"]
    shim = shutil.which("claude")
    if shim:
        candidates.insert(0, Path(shim).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe")
    return next((str(c) for c in candidates if c.is_file()), None)


class ClaudeBackend(Backend):
    id = "claude"
    label = "Claude Code"
    capabilities = frozenset({"permission_prompts", "resume", "interrupt", "background_tasks", "skip_permissions",
                              "compact", "context"})
    permission_modes = PERMISSION_MODES

    @classmethod
    def validate(cls, cfg: dict) -> None:
        super().validate(cfg)
        cli = cfg.get("options", {}).get("cli_path")
        if cli is not None and not (isinstance(cli, str) and Path(cli).is_file()):
            raise ValueError(f"options.cli_path: not a file: {cli!r}")

    def __init__(self, host):
        super().__init__(host)
        self.client: ClaudeSDKClient | None = None
        self._reader: asyncio.Task | None = None
        self._applied: tuple | None = None          # (cwd, model, skip) the running session was started with
        self._applied_mode: str | None = None
        self._speech = SpeechStream()
        self._streamed = False                      # the current message's text arrived as deltas (and was spoken)
        self._got = False                           # the current turn has produced something
        self._outcome: bool | None = None           # how the turn ended: answered, or not at all
        self._fail_reason: str | None = None
        self._result = asyncio.Event()              # set when the turn's ResultMessage has been handled
        self._turn_id: str | None = None
        self._text_id: str | None = None            # the activity event of the reply being streamed
        self._text = ""
        self._text_at = 0.0
        self._cost_total = 0.0                     # the CLI's running total for its process (it reports the total)
        self._model: str | None = None              # the model the main conversation runs on (for its context window)
        self._compacting = False                    # our own /compact is running

    @property
    def cfg(self) -> dict:
        return self.host.backend_cfg

    @property
    def log(self):
        return self.host.activity

    # --- session ---------------------------------------------------------------------------------------
    def _options(self) -> ClaudeAgentOptions:
        cfg = self.cfg
        options = dict(
            cwd=cfg["cwd"], cli_path=find_cli(cfg.get("options", {}).get("cli_path")),
            system_prompt={"type": "preset", "preset": "claude_code", "append": SPOKEN + FACE_PROMPT + self._tools_prompt()},
            include_partial_messages=True, disallowed_tools=["AskUserQuestion"],
            mcp_servers=self._mcp_servers(),
            allowed_tools=[FACE_TOOL, FACE_SHOW_TOOL, FACE_SET_TOOL, *LOOK_TOOLS, *(PHONE_TOOLS if self._phone() else [])],
            resume=self.host.state.get("session_id") or None, model=cfg.get("model") or None,
            stderr=lambda line: log.debug("claude: %s", line.rstrip()))
        extra = {"chrome": None} if self._tools().get("chrome") else {}    # Claude in Chrome (the user's own Chrome)
        if cfg["skip_permissions"]:
            # nothing is ever asked: Claude Code's own bypass (the SDK then never calls can_use_tool)
            options.update(permission_mode="bypassPermissions", extra_args={**extra, "dangerously-skip-permissions": None})
        else:
            options.update(permission_mode=cfg["permission_mode"], can_use_tool=self._can_use_tool, extra_args=extra)
        return ClaudeAgentOptions(**options)

    def _tools(self) -> dict:
        return {"computer": True, "browser": True, "chrome": True, **(self.cfg.get("tools") or {})}

    def _tools_prompt(self) -> str:
        t = self._tools()
        if not (t["computer"] or t["browser"] or t["chrome"]):
            return ""
        return TOOLS_PROMPT.format(chrome=CHROME_PROMPT if t["chrome"] else "")

    def _phone(self):
        """The phone line, if it's on."""
        phone = getattr(self.host, "phone", None)
        return phone if phone is not None and self.host.cfg.get("phone", {}).get("enabled") else None

    def _mcp_servers(self) -> dict:
        servers = {"face": self._face_server()}
        if self._phone():
            servers["phone"] = self._phone_server()
        t = self._tools()
        if t["computer"] and sys.platform == "win32":
            servers["computer"] = self._computer_server()
        if t["browser"]:
            servers["browser"] = self._browser_server()
        return servers

    def _browser_server(self) -> dict:
        """Playwright's MCP server driving the installed Chrome, in a profile of the face's own (sign-ins stay)."""
        args = ["-y", PLAYWRIGHT_MCP, "--browser", "chrome"]
        profile = getattr(getattr(self.host, "profile", None), "dir", None)
        if profile:
            args += ["--user-data-dir", str(Path(profile) / "browser")]
        if sys.platform == "win32":                  # npx is a .cmd script: Claude Code starts it through cmd
            return {"type": "stdio", "command": "cmd", "args": ["/c", "npx", *args]}
        return {"type": "stdio", "command": "npx", "args": args}

    def _computer_server(self):
        """Screenshots, mouse and keyboard (``computer.py``), as tools; every action returns a fresh screenshot."""
        from ..computer import Computer

        pc = Computer()
        loop_run = lambda fn, *a: asyncio.get_running_loop().run_in_executor(None, fn, *a)

        async def after(note: str, settle: float = 0.5) -> dict:
            await asyncio.sleep(settle)                  # let the screen update before looking
            image, w, h = await loop_run(pc.screenshot)
            return {"content": [{"type": "text", "text": f"{note} Screen now ({w}x{h}):"},
                                {"type": "image", "data": image, "mimeType": "image/png"}]}

        def failed(e) -> dict:
            return {"content": [{"type": "text", "text": str(e)}], "is_error": True}

        point = {"x": {"type": "number", "description": "pixels from the left of the latest screenshot"},
                 "y": {"type": "number", "description": "pixels from the top of the latest screenshot"}}

        @tool("screenshot", "See the whole screen. Coordinates for the other computer tools are pixels in this image.",
              {"type": "object", "properties": {}})
        async def screenshot(args):
            return await after("Screenshot taken.", settle=0)

        @tool("click", "Click at a point on the screen.", {"type": "object", "properties": {
            **point, "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "default left"},
            "clicks": {"type": "integer", "description": "1 (default), 2 for a double click"}}, "required": ["x", "y"]})
        async def click(args):
            try:
                await loop_run(pc.click, args["x"], args["y"], args.get("button", "left"), args.get("clicks", 1))
            except (ValueError, KeyError) as e:
                return failed(e)
            return await after(f"Clicked at ({args['x']}, {args['y']}).")

        @tool("move_mouse", "Move the mouse pointer (to hover).", {"type": "object", "properties": point, "required": ["x", "y"]})
        async def move_mouse(args):
            try:
                await loop_run(pc.move, args["x"], args["y"])
            except (ValueError, KeyError) as e:
                return failed(e)
            return await after(f"Pointer at ({args['x']}, {args['y']}).")

        @tool("drag", "Press the left button at one point, move to another and release.", {"type": "object", "properties": {
            "from_x": {"type": "number"}, "from_y": {"type": "number"}, "to_x": {"type": "number"}, "to_y": {"type": "number"}},
            "required": ["from_x", "from_y", "to_x", "to_y"]})
        async def drag(args):
            try:
                await loop_run(pc.drag, args["from_x"], args["from_y"], args["to_x"], args["to_y"])
            except (ValueError, KeyError) as e:
                return failed(e)
            return await after("Dragged.")

        @tool("scroll", "Scroll with the wheel at a point: positive amounts scroll up, negative down (notches).",
              {"type": "object", "properties": {**point, "amount": {"type": "integer"}}, "required": ["x", "y", "amount"]})
        async def scroll(args):
            try:
                await loop_run(pc.scroll, args["x"], args["y"], args["amount"])
            except (ValueError, KeyError) as e:
                return failed(e)
            return await after(f"Scrolled {args['amount']}.")

        @tool("type_text", "Type text where the keyboard focus is (click into the field first).",
              {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
        async def type_text(args):
            await loop_run(pc.type_text, args.get("text", ""))
            return await after("Typed.")

        @tool("press_key", 'Press a key or a chord, e.g. "enter", "esc", "ctrl+s", "alt+tab", "win+r".',
              {"type": "object", "properties": {"keys": {"type": "string"}}, "required": ["keys"]})
        async def press_key(args):
            try:
                await loop_run(pc.key, args.get("keys", ""))
            except ValueError as e:
                return failed(e)
            return await after(f"Pressed {args.get('keys')}.")

        return create_sdk_mcp_server("computer", tools=[screenshot, click, move_mouse, drag, scroll, type_text, press_key])

    def _face_server(self):
        """An in-process MCP server with the tools that move the face: its expression, and away or back."""
        host = self.host

        @tool("express", "Show a feeling on your animated face for a few seconds (it fades back by itself).",
              {"type": "object", "properties": {
                  "emotion": {"type": "string", "description": "happy, excited, amused, surprised, curious, thinking, "
                                                               "concerned, sad, afraid, frustrated, calm or neutral"},
                  "seconds": {"type": "number", "description": "how long to hold it, 1 to 30 (default 4)"}},
               "required": ["emotion"]})
        async def express(args):
            fn = getattr(host, "express", None)
            if fn is None:
                return {"content": [{"type": "text", "text": "There is no face to show it on."}], "is_error": True}
            try:
                shown = fn(args.get("emotion", ""), args.get("seconds", 4))
            except (ValueError, TypeError) as e:
                return {"content": [{"type": "text", "text": str(e)}], "is_error": True}
            return {"content": [{"type": "text", "text": f"Your face shows {shown}."}]}

        @tool("show_face", "Make your animated face vanish from the screen (visible false), or come back (visible true).",
              {"type": "object", "properties": {"visible": {"type": "boolean"}}, "required": ["visible"]})
        async def show_face(args):
            fn = getattr(host, "show_face", None)
            if fn is None:
                return {"content": [{"type": "text", "text": "There is no face to show or hide."}], "is_error": True}
            visible = args.get("visible") not in (False, "false", "False", 0)
            fn(visible)
            return {"content": [{"type": "text", "text": "Your face is back on screen." if visible else
                                 "Your face has gone from the screen (you are still here and working)."}]}

        portraits = {p["id"]: p["label"] for p in faceconfig.portraits()}

        @tool("set_face", "Change how your face looks: to anyone or anything the user asks for. It takes a few seconds. "
              "Or put a built-in face back on.",
              {"type": "object", "properties": {
                  "who": {"type": "string", "description": "who or what to look like: their name, capitalised ('John "
                          "Cena', 'Vision', 'John Wick'), or a few words ('a pirate captain with a grey beard', 'a friendly "
                          "brass robot')"},
                  "portrait": {"type": "string", "description": "instead, one of the built-in faces: " + ", ".join(portraits)}}})
        async def set_face(args):
            pid = str(args.get("portrait") or "").strip().lower()
            who = str(args.get("who") or args.get("description") or "").strip()
            make, wear = getattr(host, "make_face", None), getattr(host, "use_portrait", None)
            if make is None or wear is None:
                return {"content": [{"type": "text", "text": "There is no face to change."}], "is_error": True}
            if not (pid or who):
                return {"content": [{"type": "text", "text": "Say who or what to look like (who), or which built-in "
                                     "face to wear (portrait)."}], "is_error": True}
            try:
                if pid:
                    await wear(pid)
                    return {"content": [{"type": "text", "text": f"You have the built-in face {portraits.get(pid, pid)} on."}]}
                _cfg, jpeg = await make(who)
            except (FaceMakerError, ValueError) as e:
                return {"content": [{"type": "text", "text": f"Your face is unchanged: {e}"}], "is_error": True}
            return {"content": [{"type": "text", "text": "This is your face now, on screen:"},
                                {"type": "image", "data": base64.b64encode(jpeg).decode("ascii"), "mimeType": "image/jpeg"}]}

        self.face_tool = express
        self.show_face_tool = show_face
        self.set_face_tool = set_face
        return create_sdk_mcp_server("face", tools=[express, show_face, set_face])

    def _phone_server(self):
        """An in-process MCP server with the phone line's tools for the owner's agent (see phone/line.py)."""
        from ..phone.line import AGENT_TOOLS, PhoneError
        from ..phone.numbers import NumberError
        from ..phone.twilio import TwilioError

        def make(name: str, description: str, schema: dict):
            @tool(name, description, schema)
            async def run(args):
                phone = self._phone()
                if phone is None:
                    return {"content": [{"type": "text", "text": "The phone line is off (Settings > Phone)."}], "is_error": True}
                try:
                    return {"content": [{"type": "text", "text": await phone.agent_tool(name, args)}]}
                except (PhoneError, NumberError, TwilioError) as e:
                    return {"content": [{"type": "text", "text": f"Couldn't: {e}"}], "is_error": True}
            return run

        return create_sdk_mcp_server("phone", tools=[make(*t) for t in AGENT_TOOLS])

    def _snapshot(self) -> tuple:
        cfg = self.cfg
        return (cfg["cwd"], cfg.get("model") or None, bool(cfg["skip_permissions"]), tuple(sorted(self._tools().items())),
                bool(self._phone()))

    async def _connect(self):
        if self.client:
            return
        self._resumed = bool(self.host.state.get("session_id"))
        self.connecting = True
        self.host.changed()
        # what this process starts with: the settings can change while it starts (apply_config then waits for it)
        applied, mode = self._snapshot(), self.cfg["permission_mode"]
        try:
            options = self._options()
            client = ClaudeSDKClient(options)
            await client.connect()
        except Exception as e:
            self.last_error = f"Claude Code could not start: {e}"
            self.log.emit("error", summary=self.last_error)
            raise
        finally:
            self.connecting = False
            self.host.changed()
        self.client, self.connected, self.last_error = client, True, None
        self._applied, self._applied_mode = applied, mode
        self._reader = asyncio.create_task(self._read(client), name="claude-reader")
        log.info("Claude Code session started in %s (%s%s%s)", options.cwd, "resumed" if options.resume else "new",
                 f", model {options.model}" if options.model else "", ", permissions skipped" if applied[2] else "")
        self.log.emit("notice", summary="Session started" + (" (permissions skipped)" if applied[2] else ""),
                      output=f"{options.cwd}\n{'resumed' if options.resume else 'new conversation'}"
                             + (f"\nmodel: {options.model}" if options.model else ""))
        self.host.changed()
        await self.apply_config()                    # anything changed while it started: restart, or switch the mode

    async def _close(self):
        client, self.client = self.client, None
        reader, self._reader = self._reader, None
        self.connected = False
        if reader:
            reader.cancel()
        if client:
            try:
                await client.disconnect()
            except Exception:
                log.exception("closing the Claude Code session")
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
            log.exception("could not start Claude Code")
            self.host.notify(f"Claude Code could not start: {e}")

    async def run(self):
        """Owns the client: every use of it happens in this task."""
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
                    self.last_error = f"Claude Code stopped: {e}"
                    self.host.notify(self.last_error)
                    self.log.emit("error", turn_id=self._turn_id, summary=self.last_error)
                    await self._close()
                    continue
                finally:
                    self.active, self.op = False, None
                    self.host.changed()
                await self._maybe_compact()

    async def halt(self):
        self._drain()
        if self.client and self.active:
            try:
                await self.client.interrupt()
            except Exception:
                log.exception("interrupt failed")

    async def apply_config(self):
        """Folder, model and the skip-permissions flag only apply to a new process (the conversation resumes);
        the permission mode can be switched live."""
        if not self.client:
            if self.cfg.get("auto_start") and not self.connecting:
                self.reconnect()
            return
        if self._snapshot() != self._applied:
            self.reconnect()
        elif not self.cfg["skip_permissions"] and self.cfg["permission_mode"] != self._applied_mode:
            self._applied_mode = self.cfg["permission_mode"]
            try:
                await self.client.set_permission_mode(self._applied_mode)
            except Exception:
                log.exception("could not switch permission mode; reconnecting")
                self.reconnect()

    # --- its MCP servers, live (the panel's Tools tab: harness_tools.py) ------------------------------------------
    async def mcp_status(self) -> list[dict] | None:
        """Every MCP server the session knows (its own, the face's, claude.ai's...) with its status, scope and
        tools, as /mcp shows them; None while there is no session."""
        client = self.client
        if not client:
            return None
        return (await client.get_mcp_status()).get("mcpServers") or []

    async def toggle_mcp(self, name: str, enabled: bool) -> None:
        """Turn a server on or off in the session, as /mcp does: it applies at once, and Claude Code keeps it for
        the project (disabledMcpServers in ~/.claude.json)."""
        if not self.client:
            raise RuntimeError("Claude Code isn't running")
        await self.client.toggle_mcp_server(name, enabled)

    # --- one turn ----------------------------------------------------------------------------------------
    async def _attempt(self, text: str, prompt: str | None = None) -> None:
        """A saved session can be gone, belong to another folder or be too long: nothing comes back at all."""
        self._got = False
        try:
            answered = await self._turn(text, prompt)
        except Exception as e:
            if self._got:
                raise
            raise NoReply(str(e)) from e
        if not answered:
            raise NoReply(self._fail_reason or "no reply")

    async def _turn(self, text: str, prompt: str | None = None) -> bool:
        """One prompt and its whole reply; returns whether Claude Code answered at all."""
        await self._connect()
        self.active = True                          # (else its first words would look like it started on its own)
        self._got, self._outcome, self._fail_reason, self._streamed = False, None, None, False
        self._result = asyncio.Event()
        self._begin_turn(text, "user")
        await self.client.query(prompt or text)
        await self._result.wait()
        return bool(self._outcome)

    async def _compact_session(self) -> None:
        """Claude Code's own /compact command."""
        await self._connect()
        self._got, self._outcome, self._fail_reason, self._streamed = False, None, None, False
        self._result = asyncio.Event()
        self._compacting = True
        try:
            await self.client.query("/compact")
            await self._result.wait()
        finally:
            self._compacting = False
        if not self._outcome:
            raise RuntimeError(self._fail_reason or "Claude Code didn't say why")
        self.context = None                         # known again with the next reply

    def _begin_turn(self, text: str, source: str):
        text = label(text)
        self._turn_source = source
        self._turn_id = self.log.emit("turn", summary=text[:200] or "Background activity", input=text,
                                      status="running", extra={"source": source}).id
        host_hook(self.host, "turn_started", source)

    def _wake(self):
        """Claude Code started working without being asked (a background task finished, say)."""
        if not self.active:
            self.active = True
            self._got, self._outcome = False, None
            self._result = asyncio.Event()
            self._begin_turn("", "background")
            self.host.changed()

    def _abort_turn(self, reason: str):
        """The session went away under a running turn."""
        if self.active or self._turn_id:
            self._fail_reason = self._fail_reason or reason
            self._outcome = False
            self._finish_text()
            had_turn = bool(self._turn_id)
            if self._turn_id:
                self.log.finish_running(self._turn_id, "(stopped)")
                self.log.update(self._turn_id, status="error", extra={"stopped": reason})
            self._turn_id = None
            self.active, self.op = False, None
            self._result.set()
            if had_turn:
                host_hook(self.host, "turn_done", getattr(self, "_turn_source", "user"))

    # --- everything the session says (reader task) ----------------------------------------------------------
    async def _read(self, client: ClaudeSDKClient):
        ended: str | None = None
        try:
            async for msg in client.receive_messages():
                try:
                    self._handle(msg)
                except Exception:
                    log.exception("handling a message from Claude Code")
            ended = "Claude Code ended the session"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("reading from Claude Code")
            ended = f"Claude Code stopped: {e}"
        if self.client is client:                  # not our own close: the process went away
            self.client, self.connected, self._reader = None, False, None
            self.last_error = ended
            self.tasks.clear()
            self.log.emit("error", summary=ended)
            self._abort_turn(ended)
            self.host.notify(ended)
            self.host.changed()

    def _handle(self, msg):
        if isinstance(msg, StreamEvent):
            self._on_stream(msg)
        elif isinstance(msg, AssistantMessage):
            self._on_assistant(msg)
        elif isinstance(msg, UserMessage):
            self._on_user(msg)
        elif isinstance(msg, ResultMessage):
            self._on_result(msg)
        elif isinstance(msg, TaskStartedMessage):
            self.tasks[msg.task_id] = msg.description
            self.log.emit("task", id=f"task:{msg.task_id}", turn_id=self._turn_id, parent_id=msg.tool_use_id,
                          name=msg.task_type or "task", summary=msg.description, status="running")
            self.host.changed()
        elif isinstance(msg, TaskProgressMessage):
            self.tasks[msg.task_id] = msg.description
            usage = msg.usage or {}
            self.log.update(f"task:{msg.task_id}", summary=msg.description,
                            extra={"last_tool": msg.last_tool_name, "tool_uses": usage.get("tool_uses"),
                                   "tokens": usage.get("total_tokens")})
            self.host.changed()
        elif isinstance(msg, TaskNotificationMessage):
            self._finish_task(msg.task_id, msg.status, msg.summary)
        elif isinstance(msg, TaskUpdatedMessage):
            status = msg.status or msg.patch.get("status")
            if status in TERMINAL_TASK_STATUSES:
                self._finish_task(msg.task_id, status, "")
        elif isinstance(msg, SystemMessage):
            self._on_system(msg)

    def _on_system(self, msg: SystemMessage):
        """Claude Code compacting the conversation: on request (ours), or on its own as the context fills up."""
        data = msg.data or {}
        if msg.subtype == "status":
            if data.get("status") == "compacting":
                self._set_op(COMPACTING)
            elif self.op == COMPACTING:
                self._set_op(None)
        elif msg.subtype == "compact_boundary":
            meta = data.get("compact_metadata") or {}
            if self.op == COMPACTING:
                self._set_op(None)
            self.context = None
            self.host.changed()
            if meta.get("trigger") != "manual":         # ours is reported by _compact_now
                before = meta.get("pre_tokens")
                self.log.emit("notice", turn_id=self._turn_id, summary="Compacted the conversation",
                              output="Claude Code did it on its own" + (f", at {before:,} tokens." if isinstance(before, int) else "."))

    def _finish_task(self, task_id: str, status: str, summary: str):
        self.tasks.pop(task_id, None)
        fields = {"status": "ok" if status == "completed" else "error", "extra": {"final": status}}
        if summary:
            fields["output"] = summary
        self.log.update(f"task:{task_id}", **fields)
        self.host.changed()

    def _on_stream(self, msg: StreamEvent):
        if msg.parent_tool_use_id:                 # a subagent's stream: not for the user's ears
            return
        ev = msg.event
        kind = ev.get("type")
        if kind == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
            self._wake()
            self._got = self._streamed = True
            self._set_op(None)
            delta = ev["delta"].get("text", "")
            self._add_text(delta)
            self.host.speak(self._speech.feed(delta))
        elif kind in ("content_block_stop", "message_stop"):
            self.host.speak(self._speech.flush())
            self._finish_text()

    def _add_text(self, delta: str):
        """A reply as it streams: one activity event, refreshed a few times a second."""
        self._text += delta
        now = time.monotonic()
        if self._text_id is None:
            self._text_id = self.log.emit("assistant", turn_id=self._turn_id, status="running", output=self._text).id
            self._text_at = now
        elif now - self._text_at >= TEXT_UPDATE_S:
            self._text_at = now
            self.log.update(self._text_id, output=self._text)

    def _finish_text(self, final: str | None = None):
        if self._text_id is not None:
            self.log.update(self._text_id, output=final if final is not None else self._text, status="ok")
        self._text_id, self._text = None, ""

    def _on_assistant(self, msg: AssistantMessage):
        parent = msg.parent_tool_use_id
        if not parent:
            self._wake()
            self._got = True
            self._track_context(msg)
        for block in msg.content:
            if isinstance(block, TextBlock):
                if parent:
                    self.log.emit("assistant", turn_id=self._turn_id, parent_id=parent, output=block.text)
                elif self._streamed and self._text_id is not None:
                    self._finish_text(block.text)
                elif not self._streamed:
                    self.log.emit("assistant", turn_id=self._turn_id, output=block.text)
                    self.host.speak(block.text)
            elif isinstance(block, ThinkingBlock):
                if block.thinking.strip():
                    self.log.emit("thinking", turn_id=self._turn_id, parent_id=parent, output=block.thinking)
            elif isinstance(block, ToolUseBlock):
                args = block.input or {}
                self.log.emit("tool", id=block.id, turn_id=self._turn_id, parent_id=parent, name=block.name,
                              summary=tool_target(block.name, args), status="running",
                              input=json.dumps(args, indent=2, default=str, ensure_ascii=False))
                if not parent:
                    self._set_op(describe_tool(block.name, args))
        if not parent:
            self._streamed = False

    def _track_context(self, msg: AssistantMessage):
        """What this message's request held: the whole conversation so far, as the model saw it."""
        u = msg.usage or {}
        used = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
                                                 "output_tokens"))
        if used:
            self._model = msg.model or self._model
            self.context = {"used": used, "size": (self.context or {}).get("size")}

    def _on_user(self, msg: UserMessage):
        if not isinstance(msg.content, list):
            return
        for block in msg.content:
            if isinstance(block, ToolResultBlock):
                self.log.update(block.tool_use_id, status="error" if block.is_error else "ok",
                                output=result_text(block.content))

    def _on_result(self, msg: ResultMessage):
        self.host.speak(self._speech.flush())
        self._finish_text()
        if msg.session_id and msg.session_id != self.host.state.get("session_id"):
            self.host.state["session_id"] = msg.session_id
            self.host.save_state()
        interrupted = msg.subtype == "error_during_execution"      # our own halt
        failed = msg.is_error and not interrupted
        if failed:
            detail = msg.result or "; ".join(msg.errors or []) or msg.subtype
            log.warning("turn ended with an error: %s", detail)
            self._fail_reason = str(detail)[:300]
            if not self._compacting:                # _compact_now reports that one
                self.log.emit("error", turn_id=self._turn_id, summary=str(detail)[:300])
            if self._got:
                self.host.notify(str(detail)[:300])
        self._outcome = not (failed and not self._got)
        if self._outcome:
            self._got = True
            self.last_error = None
            self._resumed = False
        windows = {name: (mu or {}).get("contextWindow") for name, mu in (msg.model_usage or {}).items()}
        window = windows.get(self._model) or max((w for w in windows.values() if isinstance(w, int)), default=None)
        if self.context and isinstance(window, int) and window > 0:
            self.context = {**self.context, "size": window}
        cost = None
        if msg.total_cost_usd is not None:
            total = float(msg.total_cost_usd)
            # total_cost_usd is the session's running total; a new CLI process starts it again from zero
            cost = total - self._cost_total if total >= self._cost_total else total
            self._cost_total = total
            self.session_cost = (self.session_cost or 0.0) + cost
        if self._turn_id:
            self.log.finish_running(self._turn_id, "(interrupted)" if interrupted else "(no result)")
            usage = msg.usage or {}
            self.log.update(self._turn_id, status="error" if failed else "ok", duration_ms=msg.duration_ms,
                            extra={"cost_usd": None if cost is None else round(cost, 6),
                                   "session_cost_usd": None if self.session_cost is None else round(self.session_cost, 6),
                                   "turns": msg.num_turns, "interrupted": interrupted,
                                   "tokens": (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)})
        had_turn = bool(self._turn_id)
        self._turn_id = None
        self.active, self.op = False, None
        self._result.set()
        if had_turn:
            host_hook(self.host, "turn_done", getattr(self, "_turn_source", "user"))
        self.host.changed()

    def _set_op(self, op: str | None):
        if op != self.op:
            self.op = op
            self.host.changed()

    async def _can_use_tool(self, name: str, args: dict, context):
        show, say = describe_request(name, args)
        suggestions = list(getattr(context, "suggestions", None) or [])
        ev = self.log.emit("permission", turn_id=self._turn_id, name=name, summary=show, status="running")
        allow, always = await self.host.ask_permission(show, say, bool(suggestions))
        self.log.update(ev.id, status="ok" if allow else "error",
                        output=("allowed" + (" for the session" if always else "")) if allow else "denied")
        if not allow:
            return PermissionResultDeny(message="The user said no to this.")
        updates = None
        if always and suggestions:          # the CLI's own "don't ask again" rule, kept to this session
            updates = [dataclasses.replace(s, destination="session") for s in suggestions]
        return PermissionResultAllow(updated_input=args, updated_permissions=updates)
