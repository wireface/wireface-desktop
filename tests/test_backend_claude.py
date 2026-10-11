import asyncio
import json
from types import SimpleNamespace

import pytest
from claude_agent_sdk import (AssistantMessage, ResultMessage, SystemMessage, TaskNotificationMessage,
                              TaskStartedMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage)
from claude_agent_sdk.types import StreamEvent

from wireface import profile as profiles
from wireface.activity import ActivityLog
from wireface.backends import registry
from wireface.backends.base import Backend, NoReply
from wireface.backends.claude import ClaudeBackend, result_text, tool_target


class Host:
    """What the App gives a backend."""

    def __init__(self, tmp_path, **backend):
        self.backend_cfg = {**profiles.defaults()["backend"], "cwd": str(tmp_path), **backend}
        self.state = {"session_id": None}
        self.activity = ActivityLog()
        self.spoken, self.notices, self.changes, self.saved = [], [], 0, 0

    def speak(self, text): self.spoken.append(text)
    def notify(self, text): self.notices.append(text)
    def changed(self): self.changes += 1
    def save_state(self): self.saved += 1
    async def ask_permission(self, show, say, can_always): return True, False


def delta(text, parent=None):
    return StreamEvent("u", "s", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}, parent)


def result(**kw):
    return ResultMessage(**{"subtype": "success", "duration_ms": 1200, "duration_api_ms": 1000, "is_error": False,
                            "num_turns": 2, "session_id": "sess-1", "total_cost_usd": 0.01,
                            "usage": {"input_tokens": 10, "output_tokens": 5}, **kw})


@pytest.fixture
def backend(tmp_path):
    return ClaudeBackend(Host(tmp_path))


# --- skipping permissions ----------------------------------------------------------------------------------
def test_skip_permissions_starts_claude_code_with_its_bypass_flag(tmp_path):
    options = ClaudeBackend(Host(tmp_path, skip_permissions=True))._options()
    assert options.permission_mode == "bypassPermissions"
    assert options.extra_args == {"chrome": None, "dangerously-skip-permissions": None}
    assert options.can_use_tool is None                                   # nothing is ever asked


def test_without_skip_permissions_the_user_is_asked(tmp_path):
    options = ClaudeBackend(Host(tmp_path, permission_mode="acceptEdits"))._options()
    assert options.permission_mode == "acceptEdits" and options.extra_args == {"chrome": None}
    assert options.can_use_tool is not None


def test_the_agent_gets_the_computer_and_a_browser_unless_turned_off(tmp_path):
    options = ClaudeBackend(Host(tmp_path))._options()
    assert {"face", "computer", "browser"} <= set(options.mcp_servers)
    browser = options.mcp_servers["browser"]
    assert browser["command"] in ("cmd", "npx") and "--browser" in browser["args"] and "chrome" in browser["args"]
    assert "mcp__computer__screenshot" in options.allowed_tools and "mcp__computer__click" not in options.allowed_tools
    assert "mcp__computer__" in options.system_prompt["append"] and options.extra_args == {"chrome": None}
    off = ClaudeBackend(Host(tmp_path, tools={"computer": False, "browser": False, "chrome": False}))._options()
    assert set(off.mcp_servers) == {"face"} and off.extra_args == {} and "mcp__computer__" not in off.system_prompt["append"]


def test_computer_and_browser_requests_are_put_plainly():
    from wireface.backends.claude import describe_request, describe_tool
    assert describe_request("mcp__computer__click", {"x": 10, "y": 20}) == ("Click at (10, 20)", "Can I use the mouse on your screen?")
    assert describe_request("mcp__computer__type_text", {"text": "hello"})[1] == "Can I type on your computer?"
    assert describe_request("mcp__browser__browser_navigate", {"url": "https://example.com"}) ==         ("Open https://example.com", "Can I open a web page?")
    assert describe_tool("mcp__browser__browser_navigate", {"url": "https://example.com/x"}) == "opening example.com"
    assert describe_tool("mcp__computer__screenshot", {}) == "looking at the screen"


def test_the_saved_conversation_is_resumed(tmp_path):
    host = Host(tmp_path)
    host.state["session_id"] = "abc"
    assert ClaudeBackend(host)._options().resume == "abc"


# --- the agent registry -------------------------------------------------------------------------------------
def test_registry_knows_claude_and_refuses_the_rest():
    assert registry.get("claude") is ClaudeBackend
    with pytest.raises(ValueError, match="unknown backend"):
        registry.get("no-such-agent")


def test_a_stub_backend_plugs_in_without_the_claude_sdk(tmp_path):
    class Stub(Backend):
        id, label, permission_modes = "stub", "Stub", {"default": "Default"}
        async def run(self): pass
        async def halt(self): pass
        async def apply_config(self): pass
        async def close(self): pass

    registry.register("stub", lambda: Stub)
    try:
        cfg = profiles.validate({"backend": {"type": "stub"}}, profiles.defaults())
        assert cfg["backend"]["type"] == "stub"
        b = registry.get("stub")(Host(tmp_path))
        b.send("hi")
        assert b.busy and not b.connected
    finally:
        registry._BACKENDS.pop("stub")


# --- what a turn looks like in the log ---------------------------------------------------------------------------
def test_a_turn_is_logged_and_spoken(backend):
    host, log = backend.host, backend.host.activity
    backend._begin_turn("fix the bug", "user")
    backend.active = True
    backend._handle(delta("I looked. "))
    backend._handle(delta("Fixing it now."))
    backend._handle(AssistantMessage([TextBlock("I looked. Fixing it now."),
                                      ToolUseBlock("tu1", "Edit", {"file_path": "C:\\x\\app.py", "old_string": "a"})],
                                     "model"))
    assert backend.op == "editing app.py"
    backend._handle(UserMessage([ToolResultBlock("tu1", "ok, edited", False)]))
    backend._handle(result())

    turn = next(e for e in log.recent() if e["kind"] == "turn")
    assert turn["status"] == "ok" and turn["input"] == "fix the bug" and turn["duration_ms"] == 1200
    assert turn["extra"]["cost_usd"] == 0.01 and turn["extra"]["tokens"] == 15
    tool = log.get("tu1")
    assert tool.name == "Edit" and tool.summary == "C:\\x\\app.py" and tool.status == "ok" and tool.output == "ok, edited"
    reply = next(e for e in log.recent() if e["kind"] == "assistant")
    assert reply["output"] == "I looked. Fixing it now." and reply["status"] == "ok"
    assert "".join(host.spoken).replace(" ", "") == "Ilooked.Fixingitnow."
    assert host.state["session_id"] == "sess-1" and host.saved == 1
    assert not backend.active and backend._outcome is True and backend._result.is_set()


def test_a_failed_tool_is_marked(backend):
    backend._begin_turn("x", "user")
    backend._handle(AssistantMessage([ToolUseBlock("t", "Bash", {"command": "false\nsecond line"})], "m"))
    assert backend.host.activity.get("t").summary == "false"                 # one line for the list
    backend._handle(UserMessage([ToolResultBlock("t", [{"type": "text", "text": "boom"}], True)]))
    ev = backend.host.activity.get("t")
    assert ev.status == "error" and ev.output == "boom"


def test_subagent_work_is_logged_under_its_task_and_never_spoken(backend):
    backend._begin_turn("research", "user")
    backend._handle(AssistantMessage([ToolUseBlock("task1", "Task", {"description": "look around"})], "m"))
    backend._handle(delta("secret subagent chatter", parent="task1"))
    backend._handle(AssistantMessage([ToolUseBlock("inner", "Read", {"file_path": "a.py"})], "m", parent_tool_use_id="task1"))
    assert backend.host.activity.get("inner").parent_id == "task1"
    assert backend.host.spoken == []


def test_an_interrupted_turn_closes_its_open_steps(backend):
    backend._begin_turn("long job", "user")
    backend._handle(AssistantMessage([ToolUseBlock("t", "Bash", {"command": "sleep 99"})], "m"))
    backend._handle(result(subtype="error_during_execution", is_error=True))
    assert backend.host.activity.get("t").status == "error" and backend.host.activity.get("t").output == "(interrupted)"
    assert backend._outcome is True and backend.host.notices == []           # our own halt is not an error


def test_an_error_before_any_answer_fails_the_turn(backend):
    backend._begin_turn("x", "user")
    backend._handle(result(is_error=True, result="no such session"))
    assert backend._outcome is False and backend._fail_reason == "no such session"


# --- background work and the beacon ---------------------------------------------------------------------------
def test_background_tasks_are_tracked(backend):
    backend._handle(TaskStartedMessage("task_started", {}, "bg1", "run the tests", "u", "s", "tu9", "local_bash"))
    assert backend.tasks == {"bg1": "run the tests"}
    assert backend.host.activity.get("task:bg1").status == "running"
    backend._handle(TaskNotificationMessage("task_notification", {}, "bg1", "completed", "out.txt", "all passed", "u", "s"))
    assert backend.tasks == {}
    ev = backend.host.activity.get("task:bg1")
    assert ev.status == "ok" and ev.output == "all passed"


def test_claude_speaking_unprompted_starts_a_background_turn(backend):
    backend._handle(delta("The tests finished."))
    assert backend.active
    turn = next(e for e in backend.host.activity.recent() if e["kind"] == "turn")
    assert turn["extra"]["source"] == "background"
    backend._handle(result())
    assert not backend.active


def test_turn_survives_the_session_dying(backend):
    backend._begin_turn("x", "user")
    backend.active = True
    backend._abort_turn("Claude Code stopped")
    assert not backend.active and backend._outcome is False and backend._result.is_set()
    assert backend.host.activity.get(backend._turn_id or "none") is None


def test_helpers():
    assert tool_target("Bash", {"command": "ls -la\nmore"}) == "ls -la"
    assert tool_target("Grep", {"pattern": "TODO"}) == "TODO"
    assert tool_target("Mystery", {}) == ""
    assert result_text([{"type": "text", "text": "a"}, {"type": "image"}]) == "a\n[image]"
    assert result_text(None) == ""


# --- a conversation that grows too long ------------------------------------------------------------------------
class ScriptedClient:
    """Stands in for ClaudeSDKClient: each query is answered with messages, handled as the reader task would."""

    def __init__(self, backend, long=False, compact_fails=False):
        self.b, self.long, self.compact_fails, self.queries = backend, long, compact_fails, []

    async def query(self, text):
        self.queries.append(text)
        for msg in self.answer(text):
            asyncio.get_running_loop().call_soon(self.b._handle, msg)

    def answer(self, text):
        if text == "/compact":
            if self.compact_fails:
                return [result(is_error=True, result="Conversation too long to compact")]
            self.long = False
            return [SystemMessage("status", {"status": "compacting"}),
                    SystemMessage("compact_boundary", {"compact_metadata": {"trigger": "manual", "pre_tokens": 170008}}),
                    result()]
        if self.long:
            return [result(is_error=True, result="Prompt is too long")]
        reply = "Got the recap." if text.startswith("(The previous conversation") else "Hello there."
        return [AssistantMessage([TextBlock(reply)], "claude-x", usage={"input_tokens": 5, "cache_read_input_tokens": 170000,
                                                                       "output_tokens": 3}),
                result(model_usage={"claude-x": {"contextWindow": 200000}, "claude-small": {"contextWindow": 100000}})]

    async def disconnect(self): pass


class Scripted(ClaudeBackend):
    long = compact_fails = False

    async def _connect(self):
        if self.client:
            return
        resuming = bool(self.host.state.get("session_id"))
        self._resumed = resuming
        self.client = ScriptedClient(self, self.long and resuming, self.compact_fails)
        self.connected = True
        self.clients = getattr(self, "clients", []) + [self.client]


def run(coro):
    return asyncio.run(coro)


def test_a_prompt_too_long_for_the_model_compacts_the_conversation_and_is_asked_again(tmp_path):
    host = Host(tmp_path)
    host.state["session_id"] = "sess-1"
    b = Scripted(host)
    b.long = True
    run(b._ask("hi"))
    assert b.clients[0].queries == ["hi", "/compact", "hi"] and [t for t in host.spoken if t] == ["Hello there."]
    notes = [(e["summary"], e["output"]) for e in host.activity.recent() if e["kind"] == "notice"]
    assert ("Compacted the conversation", "The conversation had grown too long for the model.") in notes
    assert not [e for e in host.activity.recent() if e["kind"] == "error" and "compact" in e["summary"]]


def test_when_compacting_fails_too_the_prompt_goes_to_a_new_conversation_with_a_recap(tmp_path):
    host = Host(tmp_path)
    b = Scripted(host)
    run(b._ask("what is two and two"))                                   # something to recap
    b.long = b.compact_fails = True
    b.client = None
    run(b._ask("and now?"))
    old, new = b.clients[1], b.clients[2]
    assert old.queries == ["and now?", "/compact"] and new.queries[0].startswith("(The previous conversation")
    assert "User: what is two and two\nYou: Hello there." in new.queries[0] and new.queries[0].endswith("and now?")
    assert [t for t in host.spoken if t][-1] == "Got the recap."
    assert any(e["summary"] == "Couldn't compact the conversation: Conversation too long to compact"
               for e in host.activity.recent() if e["kind"] == "error")


def test_claude_codes_context_use_is_tracked_and_compacted_while_idle(tmp_path):
    host = Host(tmp_path)
    b = Scripted(host)
    run(b._ask("hi"))
    assert b.context == {"used": 170008, "size": 200000}                  # the main model's window, not the small one's
    run(b._maybe_compact())                                               # 85% full, compact_at 80
    assert b.clients[0].queries == ["hi", "/compact"] and b.context is None and not b.active and b.op is None
    note = [e for e in host.activity.recent() if e["kind"] == "notice"][-1]
    assert note["summary"] == "Compacted the conversation" and note["output"].endswith("Before: 170,008 of 200,000 tokens.")


def test_claude_code_compacting_on_its_own_is_shown(backend):
    backend._handle(SystemMessage("status", {"status": "compacting"}))
    assert backend.op == "compacting the conversation"
    backend.context = {"used": 190000, "size": 200000}
    backend._handle(SystemMessage("compact_boundary", {"compact_metadata": {"trigger": "auto", "pre_tokens": 190000}}))
    assert backend.op is None and backend.context is None
    note = [e for e in backend.host.activity.recent() if e["kind"] == "notice"][-1]
    assert note["output"] == "Claude Code did it on its own, at 190,000 tokens."


# --- settings changed while Claude Code starts -----------------------------------------------------------------------
class SlowSDK:
    """Stands in for ClaudeSDKClient: connecting waits until the test lets it finish."""
    started: list = []
    gate: asyncio.Event | None = None

    def __init__(self, options):
        self.options = options
        SlowSDK.started.append(options)

    async def connect(self):
        if SlowSDK.gate is not None:
            await SlowSDK.gate.wait()

    async def receive_messages(self):
        await asyncio.Event().wait()
        yield

    async def set_permission_mode(self, mode): self.mode = mode
    async def interrupt(self): pass
    async def disconnect(self): pass


@pytest.fixture
def slow_sdk(monkeypatch):
    from wireface.backends import claude as claude_mod
    monkeypatch.setattr(claude_mod, "ClaudeSDKClient", SlowSDK)
    monkeypatch.setattr(claude_mod, "find_cli", lambda *_: None)
    SlowSDK.started, SlowSDK.gate = [], None
    return SlowSDK


async def until(cond, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


def test_a_model_chosen_while_claude_code_starts_is_not_lost(tmp_path, slow_sdk):
    host = Host(tmp_path, model="opus", auto_start=True)
    b = ClaudeBackend(host)

    async def scenario():
        slow_sdk.gate = asyncio.Event()
        task = asyncio.create_task(b.run())
        await until(lambda: b.connecting)
        assert b.applying                                     # starting: the settings aren't in force yet
        host.backend_cfg["model"] = "sonnet"                  # chosen while it starts
        await b.apply_config()
        slow_sdk.gate.set()
        await until(lambda: len(slow_sdk.started) == 2 and b.connected and not b.applying)
        assert [o.model for o in slow_sdk.started] == ["opus", "sonnet"]
        assert b._applied[1] == "sonnet"
        b.shutdown()
        await task

    run(scenario())


def test_one_restart_takes_every_change_made_before_it_begins(tmp_path, slow_sdk):
    host = Host(tmp_path, auto_start=True)
    b = ClaudeBackend(host)

    async def scenario():
        task = asyncio.create_task(b.run())
        await until(lambda: b.connected and not b.applying)
        b.active = True                                       # a reply in progress, which "stop" interrupts
        host.backend_cfg["model"] = "opus"
        await b.apply_config()
        host.backend_cfg["model"] = "sonnet"
        await b.apply_config()
        assert b.applying and b.inbox.qsize() == 1
        await b.halt()                                        # "stop" forgets prompts, not the restart
        assert b.inbox.qsize() == 1
        b.active = False
        await until(lambda: len(slow_sdk.started) == 2 and not b.applying)
        assert [o.model for o in slow_sdk.started] == [None, "sonnet"]
        b.shutdown()
        await task

    run(scenario())


def test_a_permission_mode_chosen_while_it_starts_is_switched_to(tmp_path, slow_sdk):
    host = Host(tmp_path, permission_mode="default", auto_start=True)
    b = ClaudeBackend(host)

    async def scenario():
        slow_sdk.gate = asyncio.Event()
        task = asyncio.create_task(b.run())
        await until(lambda: b.connecting)
        host.backend_cfg["permission_mode"] = "acceptEdits"
        slow_sdk.gate.set()
        await until(lambda: b.connected and not b.applying)
        assert len(slow_sdk.started) == 1 and b.client.mode == "acceptEdits" and b._applied_mode == "acceptEdits"
        b.shutdown()
        await task

    run(scenario())


# --- the beacon, from the App ---------------------------------------------------------------------------------
@pytest.fixture
def app(tmp_path, monkeypatch):
    from wireface import config
    from wireface.app import App
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "DEFAULT_CWD", tmp_path)
    return App(profiles.resolve("beacon-test"), shell=False)


def test_beacon_follows_the_agent(app):
    b = app.backend
    assert app.beacon()["state"] == "offline"
    b.connecting = True
    assert app.beacon()["state"] == "connecting"
    b.connecting, b.connected = False, True
    assert app.beacon()["state"] == "online"
    b.tasks["x"] = "tests"
    assert app.beacon()["state"] == "background" and app.beacon()["tasks"] == 1
    b.active, b.op = True, "reading app.py"
    assert app.beacon() == {"state": "working", "label": "reading app.py", "tasks": 1, "skip_permissions": False}
    app.permission = {"id": "1", "text": "Run: x", "can_always": False}
    assert app.beacon()["state"] == "needs_input"
    app.permission, b.connected, b.last_error = None, False, "Claude Code stopped"
    assert app.beacon()["state"] == "error"


def test_the_face_is_told_while_the_agent_compacts(app):
    from wireface.backends.base import COMPACTING
    b = app.backend
    b.connected, b.active, b.op = True, True, COMPACTING
    s = app.status()
    assert s["compacting"] is True and s["beacon"]["label"] == COMPACTING
    b.active, b.op = False, None
    assert app.status()["compacting"] is False


def test_beacon_shows_when_permissions_are_skipped(app):
    asyncio.run(app.set_config({"backend": {"skip_permissions": True}}))
    assert app.beacon()["skip_permissions"] is True and app.status()["skip_permissions"] is True
    assert app.profile.file.exists()                                       # saved to this face's own file


def test_the_panel_can_see_a_restart_for_new_settings_until_it_is_done(app):
    b = app.backend
    b.applying = False                                                     # as after its first start
    asyncio.run(app.set_config({"backend": {"model": "sonnet", "tools": {"computer": False, "browser": True, "chrome": True}}}))
    assert app.config_view()["backend"]["applying"] is True and app.status()["backend"]["applying"] is True
    assert b.inbox.qsize() == 1                                            # one restart for both changes
    assert app.cfg["backend"]["model"] == "sonnet" and app.cfg["backend"]["tools"]["computer"] is False


def test_settings_changes_are_validated_and_saved(app):
    with pytest.raises(ValueError):
        asyncio.run(app.set_config({"backend": {"permission_mode": "bogus"}}))
    view = app.config_view()
    assert '"api_key"' not in json.dumps(view) and view["backend"]["type"] == "claude"   # only the variable's name


class Stub2(Backend):
    id, label, permission_modes = "stub2", "Stub", {"default": "Default", "strict": "Strict"}

    async def run(self):
        while True:
            kind, _ = await self.inbox.get()
            if kind == "quit":
                return

    async def halt(self): pass
    async def apply_config(self): pass
    async def close(self): pass


def test_the_agent_can_be_switched_live_and_each_keeps_its_conversation(app):
    registry.register("stub2", lambda: Stub2)

    async def scenario():
        app.cfg["backend"]["auto_start"] = False           # no real Claude Code in a test
        app.start_backend()
        await app.set_config({"backend": {"permission_mode": "acceptEdits"}})
        app.state["session_id"] = "claude-conversation"
        claude = app.backend

        await app.set_config({"backend": {"type": "stub2"}})
        assert isinstance(app.backend, Stub2) and app.backend is not claude
        assert app.cfg["backend"]["permission_mode"] == "default"             # acceptEdits means nothing to the stub
        assert app.state["session_id"] is None and app.state["sessions"] == {"claude": "claude-conversation"}
        assert app.status()["backend"]["label"] == "Stub" and list(app.status()["permission_modes"]) == ["default", "strict"]

        app.state["session_id"] = "stub-conversation"
        await app.set_config({"backend": {"type": "claude"}})
        assert app.backend.id == "claude" and app.state["session_id"] == "claude-conversation"
        assert app.state["sessions"]["stub2"] == "stub-conversation"
        app.backend.shutdown()
        await app._backend_task

    try:
        asyncio.run(scenario())
    finally:
        registry._BACKENDS.pop("stub2")
    assert json.loads(app.profile.state_file.read_text())["sessions"]["stub2"] == "stub-conversation"
    assert json.loads(app.profile.file.read_text())["backend"]["type"] == "claude"
