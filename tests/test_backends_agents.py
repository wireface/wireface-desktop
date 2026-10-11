"""OpenCode (ACP) and Codex (exec --json) backends, against stand-in agents (tests/fake_acp_agent.py, fake_codex.py)."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

from wireface import profile as profiles
from wireface.activity import ActivityLog
from wireface.backends import base, codex as codex_mod, exe, registry
from wireface.backends.base import NoReply
from wireface.backends.acp import AcpError, OpenCodeBackend, acp_op, acp_request_text
from wireface.backends.codex import CodexBackend, change_summary, command_summary

HERE = Path(__file__).parent
FAKE_ACP, FAKE_CODEX = str(HERE / "fake_acp_agent.py"), str(HERE / "fake_codex.py")


class Host:
    """What the App gives a backend."""

    def __init__(self, tmp_path, **backend):
        self.backend_cfg = {**profiles.defaults()["backend"], "cwd": str(tmp_path), **backend}
        self.state = {"session_id": None, "sessions": {}}
        self.activity = ActivityLog()
        self.spoken, self.notices, self.asked, self.saved = [], [], [], 0
        self.answer = (True, False)

    def speak(self, text):
        if text:                                                          # as App.speak does
            self.spoken.append(text)
    def notify(self, text): self.notices.append(text)
    def changed(self): pass
    def save_state(self): self.saved += 1

    async def ask_permission(self, show, say, can_always):
        self.asked.append((show, say, can_always))
        return self.answer


class FakeOpenCode(OpenCodeBackend):
    def _command(self):
        return [sys.executable, FAKE_ACP]


class FakeCodex(CodexBackend):
    def _command(self, resume):
        return [sys.executable, FAKE_CODEX] + (["--resume", resume] if resume else [])

    def _app_server_command(self):
        return [sys.executable, FAKE_CODEX, "app-server"]


def events(backend, kind):
    return [e for e in backend.host.activity.recent() if e["kind"] == kind]


def turn_event(backend):
    (turn,) = events(backend, "turn")
    return turn


def drive(backend, *prompts):
    """Connect, run each prompt as a turn, close."""
    async def go():
        try:
            for prompt in prompts:
                await backend._turn(prompt) if isinstance(backend, FakeOpenCode) else await backend._ask(prompt)
        finally:
            await backend.close()
    asyncio.run(go())


# --- AGENTS.md ---------------------------------------------------------------------------------------------------
def test_agents_md_is_created_when_missing_and_never_touched_when_present(tmp_path):
    assert base.ensure_instructions(tmp_path) is True
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith("# Instructions for the face") and "read aloud" in text
    (tmp_path / "AGENTS.md").write_text("mine", encoding="utf-8")
    assert base.ensure_instructions(tmp_path) is False
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "mine"


def test_a_folder_that_cannot_be_written_is_not_fatal(tmp_path):
    assert base.ensure_instructions(tmp_path / "does" / "not" / "exist") is False


# --- OpenCode ----------------------------------------------------------------------------------------------------
def test_opencode_turn_is_spoken_logged_and_remembered(tmp_path):
    b = FakeOpenCode(Host(tmp_path))
    drive(b, "hi")
    assert b.host.spoken == ["Hello there."]
    turn = turn_event(b)
    assert turn["status"] == "ok" and turn["extra"]["tokens"] == 10 and turn["extra"]["cost_usd"] == 0.002
    (reply,) = events(b, "assistant")
    assert reply["output"] == "Hello there." and reply["status"] == "ok"
    assert b.host.state["session_id"] == "fake-1" and b.host.saved >= 1
    assert (tmp_path / "AGENTS.md").exists()                              # created at launch
    assert any(n["summary"] == "Created AGENTS.md" for n in events(b, "notice"))


def test_an_existing_agents_md_is_left_alone(tmp_path):
    (tmp_path / "AGENTS.md").write_text("project rules", encoding="utf-8")
    drive(FakeOpenCode(Host(tmp_path)), "hi")
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "project rules"


def test_opencode_asks_before_a_tool_runs_and_passes_the_answer_on(tmp_path):
    b = FakeOpenCode(Host(tmp_path))
    drive(b, "use a tool")
    (show, say, can_always), = b.host.asked
    assert show == "Edit C:\\work\\hello.txt" and say == "Can I edit hello.txt?" and can_always is True
    tool = b.host.activity.get("tc1")
    assert tool.name == "write" and tool.summary == "C:\\work\\hello.txt" and tool.status == "ok"
    assert tool.output == "Created file successfully" and "hi" in tool.input
    assert "Chose once." in events(b, "assistant")[0]["output"]
    assert events(b, "permission")[0]["status"] == "ok" and events(b, "thinking")[0]["output"] == "Let me write it."


def test_always_and_deny_choose_the_matching_options(tmp_path):
    b = FakeOpenCode(Host(tmp_path))
    b.host.answer = (True, True)
    drive(b, "use a tool")
    assert "Chose always." in events(b, "assistant")[0]["output"]
    d = FakeOpenCode(Host(tmp_path))
    d.host.answer = (False, False)
    drive(d, "use a tool")
    assert "Chose reject." in events(d, "assistant")[0]["output"]
    assert d.host.activity.get("tc1").status == "error" and events(d, "permission")[0]["status"] == "error"


def test_skipping_permissions_answers_them_without_asking(tmp_path):
    b = FakeOpenCode(Host(tmp_path, skip_permissions=True))
    drive(b, "use a tool")
    assert b.host.asked == []
    assert "Chose once." in events(b, "assistant")[0]["output"]
    assert "skipped" in events(b, "permission")[0]["output"]


def test_halting_cancels_the_turn_without_an_error(tmp_path):
    b = FakeOpenCode(Host(tmp_path))

    async def go():
        try:
            await b._connect()
            task = asyncio.create_task(b._turn("slow one"))
            for _ in range(200):
                await asyncio.sleep(0.05)
                if b._got:
                    break
            await b.halt()
            await asyncio.wait_for(task, 10)
        finally:
            await b.close()

    asyncio.run(go())
    turn = turn_event(b)
    assert turn["extra"]["interrupted"] is True and turn["status"] == "ok" and b.host.notices == []
    assert not b.active


def test_the_saved_conversation_is_resumed_without_replaying_it_aloud(tmp_path):
    host = Host(tmp_path)
    host.state["session_id"] = "old-session"
    b = FakeOpenCode(host)
    drive(b, "hi")
    assert host.state["session_id"] == "old-session"
    assert host.spoken == ["Hello there."]                                # not "OLD HISTORY."
    assert not any("OLD" in (e["output"] or "") for e in events(b, "assistant"))


def test_a_conversation_the_agent_has_lost_starts_a_new_one(tmp_path):
    host = Host(tmp_path)
    host.state["session_id"] = "gone"
    drive(FakeOpenCode(host), "hi")
    assert host.state["session_id"] == "fake-1" and host.spoken == ["Hello there."]


def ask(backend, *prompts, after=None):
    """Each prompt as the face sends it (with recovery), then ``after(backend)`` if given, then close."""
    async def go():
        try:
            for prompt in prompts:
                await backend._ask(prompt)
            if after:
                await after(backend)
        finally:
            await backend.close()
    asyncio.run(go())


def notices(backend) -> list[tuple[str, str]]:
    return [(n["summary"], n["output"] or "") for n in events(backend, "notice")]


def test_a_conversation_too_long_for_the_model_is_compacted_and_the_prompt_asked_again(tmp_path):
    host = Host(tmp_path)
    host.state["session_id"] = "long"
    b = FakeOpenCode(host)
    ask(b, "hi")
    assert host.state["session_id"] == "long" and host.spoken == ["Hello there."]
    assert ("Compacted the conversation", "The conversation had grown too long for the model.") in notices(b)
    assert not any(s.startswith("Started a new conversation") for s, _ in notices(b))


def test_a_conversation_that_cant_even_be_compacted_goes_on_in_a_new_one_with_a_recap(tmp_path):
    host = Host(tmp_path)
    b = FakeOpenCode(host)
    ask(b, "hi")                                                          # something to recap
    host.state["session_id"] = "too-big"
    ask(b, "and now?")
    assert host.state["session_id"] == "fake-1" and host.spoken[-1] == "Got the recap."
    assert any(e["summary"].startswith("Couldn't compact the conversation") and "8 MB" in e["summary"]
               for e in events(b, "error"))
    (summary, detail), = [n for n in notices(b) if n[0].startswith("Started a new conversation")]
    assert summary == "Started a new conversation, with a recap of the last one" and "8 MB" in detail
    assert [t["input"] for t in events(b, "turn")][-1] == "and now?"     # logged as asked, without the recap


def test_the_old_conversation_is_kept_when_a_new_one_fails_too(tmp_path):
    host = Host(tmp_path)
    host.state["session_id"] = "old-session"
    with pytest.raises(NoReply, match="something broke"):
        ask(FakeOpenCode(host), "broken")
    assert host.state["session_id"] == "old-session"                     # the trouble wasn't the conversation


def test_a_session_that_has_answered_is_not_replaced_when_a_prompt_fails(tmp_path):
    host = Host(tmp_path)
    host.state["session_id"] = "old-session"
    b = FakeOpenCode(host)

    async def broken(b):
        with pytest.raises(NoReply):
            await b._ask("broken")
    ask(b, "hi", after=broken)
    assert host.state["session_id"] == "old-session" and not any(s.startswith("Started") for s, _ in notices(b))


def test_opencode_reports_how_full_the_context_is_and_compacts_while_idle(tmp_path):
    host = Host(tmp_path)
    b = FakeOpenCode(host)
    seen = []

    async def idle(b):
        seen.append(dict(b.context))
        await b._maybe_compact()
    ask(b, "fill", after=idle)
    assert seen == [{"used": 90, "size": 100}] and b.context is None    # compacted: known again with the next prompt
    assert ("Compacted the conversation", "It was getting full: compacted while idle, so it doesn't happen in the middle "
            "of a reply. Before: 90 of 100 tokens.") in notices(b)


def test_no_idle_compaction_below_the_mark_or_when_turned_off(tmp_path):
    for cfg, prompt in (({}, "hi"), ({"compact_at": 0}, "fill")):
        b = FakeOpenCode(Host(tmp_path, **cfg))
        ask(b, prompt, after=lambda b: b._maybe_compact())
        assert b.context and not any(s == "Compacted the conversation" for s, _ in notices(b))


def test_compacting_on_request_and_when_opencode_does_it_itself(tmp_path):
    b = FakeOpenCode(Host(tmp_path))
    ask(b, "own compaction", after=lambda b: b._compact_now("asked"))
    assert notices(b)[-2:] == [("Compacted the conversation", "OpenCode did it on its own (auto)."),
                               ("Compacted the conversation", "On request. Before: 10 of 100 tokens.")]
    assert b.op is None and not b.active


def test_nothing_to_compact_before_the_conversation_starts(tmp_path):
    b = FakeOpenCode(Host(tmp_path, auto_start=False))
    assert asyncio.run(b._compact_now("asked")) is False
    assert notices(b) == [("Nothing to compact yet", "The conversation hasn't started.")]


def test_a_crashing_agent_is_reported_and_the_beacon_state_follows(tmp_path):
    b = FakeOpenCode(Host(tmp_path))

    async def go():
        task = asyncio.create_task(b.run())
        for _ in range(100):                                              # auto_start brings it up
            await asyncio.sleep(0.05)
            if b.connected:
                break
        assert b.connected
        b.send("crash now")
        for _ in range(200):
            await asyncio.sleep(0.05)
            if b.last_error:
                break
        b.shutdown()
        await asyncio.wait_for(task, 10)

    asyncio.run(go())
    assert "stopped" in b.last_error and not b.connected and not b.active
    assert b.host.notices and events(b, "error")


def test_provider_retries_are_shown_not_mistaken_for_an_error(tmp_path):
    b = FakeOpenCode(Host(tmp_path))
    drive(b, "retry please")
    notes = [n for n in events(b, "notice") if n["summary"].startswith("The model provider")]
    assert notes and "slow down" in notes[0]["output"] and turn_event(b)["status"] == "ok" and b.op is None


def test_opencode_environment_carries_model_and_permission_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)

    def inline(**cfg):
        env = OpenCodeBackend(Host(tmp_path, **cfg))._env()
        return json.loads(env["OPENCODE_CONFIG_CONTENT"]) if "OPENCODE_CONFIG_CONTENT" in env else None

    assert inline() == {"permission": {"edit": "ask", "bash": "ask"}}
    assert inline(permission_mode="own") is None                           # its own settings
    assert inline(skip_permissions=True) is None
    assert inline(model="openrouter/x", permission_mode="own") == {"model": "openrouter/x"}
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", json.dumps({"theme": "dark", "permission": {"webfetch": "deny"}}))
    assert inline() == {"theme": "dark", "permission": {"webfetch": "deny", "edit": "ask", "bash": "ask"}}


def test_opencode_restarts_with_a_model_chosen_while_it_was_starting(tmp_path):
    host = Host(tmp_path, auto_start=True, model="a/one")
    models = []

    class Changing(FakeOpenCode):
        def _command(self):
            models.append(self.cfg["model"])
            if len(models) == 1:
                host.backend_cfg["model"] = "b/two"            # chosen while the first process starts
            return super()._command()

    b = Changing(host)

    async def go():
        task = asyncio.create_task(b.run())
        for _ in range(400):
            await asyncio.sleep(0.05)
            if len(models) == 2 and b.connected and not b.applying:
                break
        b.shutdown()
        await asyncio.wait_for(task, 10)

    asyncio.run(go())
    assert models == ["a/one", "b/two"] and b._applied[1] == "b/two"


def test_opencode_command_and_option_validation(tmp_path):
    host = Host(tmp_path, options={"cli_path": sys.executable})
    assert OpenCodeBackend(host)._command() == [sys.executable, "acp"]
    host = Host(tmp_path, options={"cli_path": sys.executable, "cli_args": ["acp", "--print-logs"]})
    assert OpenCodeBackend(host)._command() == [sys.executable, "acp", "--print-logs"]
    for bad in ({"cli_path": "Z:\\no\\such\\file.exe"}, {"cli_args": "acp"}):
        with pytest.raises(ValueError):
            profiles.validate({"backend": {"type": "opencode", "options": bad}}, profiles.defaults())


def test_acp_descriptions():
    assert acp_op("write", "edit", "C:\\x\\app.py") == "editing app.py" and acp_op("bash", "execute", "") == "running a command"
    assert acp_request_text({"kind": "execute", "title": "bash", "rawInput": {"command": "rm -rf build"}}) == (
        "Run: rm -rf build", "Can I run a command?")
    assert acp_request_text({"kind": "mystery", "title": "make_coffee"}) == ("Use make_coffee", "Can I use make coffee?")


# --- Codex -------------------------------------------------------------------------------------------------------
def test_codex_turn_is_spoken_logged_and_remembered(tmp_path):
    b = FakeCodex(Host(tmp_path))
    drive(b, "hi")
    assert b.host.spoken == ["Hello there."]
    turn = turn_event(b)
    assert turn["status"] == "ok" and turn["extra"]["tokens"] == 120
    assert b.host.state["session_id"] == "thread-1"
    assert (tmp_path / "AGENTS.md").exists()


def test_codex_commands_file_changes_and_plans_are_logged(tmp_path):
    b = FakeCodex(Host(tmp_path))
    drive(b, "use a tool")
    shell = b.host.activity.get("i2")
    assert shell.name == "shell" and shell.summary == "Set-Content hello.txt hi" and shell.status == "ok" and shell.output == "ok"
    patch = b.host.activity.get("i3")
    assert patch.name == "apply_patch" and patch.summary == "note.txt (add)" and patch.status == "ok"
    failed = b.host.activity.get("i4")
    assert failed.status == "error" and failed.output == "boom"
    plan = next(n for n in events(b, "notice") if n["summary"] == "Plan")
    assert plan["output"] == "[x] write\n[ ] test"
    assert b.host.spoken == ["I will create the file.", "Done."]
    assert turn_event(b)["status"] == "ok"


def test_codex_turn_that_fails_before_answering_is_an_error(tmp_path):
    b = FakeCodex(Host(tmp_path))
    with pytest.raises(RuntimeError, match="the model is unavailable"):
        asyncio.run(b._turn("fail please", None))
    assert turn_event(b)["status"] == "error"


def test_codex_run_loop_reports_a_failure_and_carries_on(tmp_path):
    b = FakeCodex(Host(tmp_path))

    async def go():
        task = asyncio.create_task(b.run())
        b.send("fail please")
        for _ in range(200):
            await asyncio.sleep(0.05)
            if b.last_error:
                break
        b.send("hello again")
        for _ in range(200):
            await asyncio.sleep(0.05)
            if b.host.spoken:
                break
        b.shutdown()
        await asyncio.wait_for(task, 10)

    asyncio.run(go())
    assert "the model is unavailable" in "".join(b.host.notices) and b.host.spoken == ["Hello there."]


def test_codex_config_warnings_are_shown_once(tmp_path):
    b = FakeCodex(Host(tmp_path))
    drive(b, "warn one", "warn two")
    assert len([n for n in events(b, "notice") if "unrecognized configuration" in n["summary"]]) == 1


def test_codex_halt_stops_a_running_turn(tmp_path):
    b = FakeCodex(Host(tmp_path))

    async def go():
        try:
            await b._connect()
            task = asyncio.create_task(b._ask("slow one"))
            for _ in range(200):
                await asyncio.sleep(0.05)
                if b._got:
                    break
            await b.halt()
            await asyncio.wait_for(task, 15)
        finally:
            await b.close()

    asyncio.run(go())
    assert turn_event(b)["extra"]["interrupted"] is True and b.host.notices == []


def test_codex_out_of_room_compacts_through_its_app_server_and_asks_again(tmp_path):
    host = Host(tmp_path)
    host.state["session_id"] = "long"
    b = FakeCodex(host)
    ask(b, "hi")
    assert host.state["session_id"] == "long" and host.spoken == ["Hello there."]
    assert (tmp_path / "compacted-long").exists()
    assert ("Compacted the conversation", "The conversation had grown too long for the model.") in notices(b)


def test_codex_compacts_on_request_and_starts_over_with_a_recap_when_it_cant(tmp_path):
    host = Host(tmp_path)
    b = FakeCodex(host)
    ask(b, "hi", after=lambda b: b._compact_now("asked"))
    assert (tmp_path / "compacted-thread-1").exists() and notices(b)[-1] == ("Compacted the conversation", "On request.")
    host.state["session_id"] = "too-big"
    ask(b, "and now?")
    assert host.spoken[-1] == "Got the recap." and host.state["session_id"] == "thread-1"
    assert any(s == "Started a new conversation, with a recap of the last one" for s, _ in notices(b))


def test_codex_resumes_and_recovers_from_a_lost_conversation(tmp_path):
    host = Host(tmp_path)
    host.state["session_id"] = "thread-9"
    drive(FakeCodex(host), "hi")
    assert host.state["session_id"] == "thread-9"                         # resumed
    lost = Host(tmp_path)
    lost.state["session_id"] = "gone"
    drive(FakeCodex(lost), "hi")
    assert lost.state["session_id"] == "thread-1" and lost.spoken == ["Hello there."]


def test_codex_command_line(tmp_path):
    def argv(resume=None, **cfg):
        return CodexBackend(Host(tmp_path, options={"cli_path": sys.executable}, **cfg))._command(resume)

    exe_ = sys.executable
    assert argv() == [exe_, "exec", "--json", "--skip-git-repo-check", "-c", 'sandbox_mode="workspace-write"', "-"]
    assert argv("T1") == [exe_, "exec", "resume", "--json", "--skip-git-repo-check", "-c", 'sandbox_mode="workspace-write"', "T1", "-"]
    assert "read-only" in argv(permission_mode="read-only")[5]
    assert argv(model="gpt-5")[4:6] == ["-m", "gpt-5"]
    skipped = argv(skip_permissions=True)
    assert "--dangerously-bypass-approvals-and-sandbox" in skipped and "-c" not in skipped
    extra = CodexBackend(Host(tmp_path, options={"cli_path": sys.executable, "cli_args": ["-c", "x=1"]}))._command("T")
    assert extra[-4:] == ["-c", "x=1", "T", "-"]


def test_codex_summaries():
    assert command_summary('"C:\\\\WINDOWS\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe" -Command "Get-ChildItem -Force"') == "Get-ChildItem -Force"
    assert command_summary("git status\nsecond") == "git status"
    assert change_summary([{"path": "C:\\a\\b.py", "kind": "update"}, {"path": "c.txt", "kind": "add"}]) == "b.py (update), c.txt (add)"


# --- finding the executables -----------------------------------------------------------------------------------------
def test_executables_are_found_inside_the_npm_install(tmp_path, monkeypatch):
    (tmp_path / "opencode.cmd").write_text("rem shim")
    real = tmp_path / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe"
    real.parent.mkdir(parents=True)
    real.write_text("exe")
    monkeypatch.setattr(exe.sys, "platform", "win32")
    monkeypatch.setattr(exe.shutil, "which", lambda n: str(tmp_path / "opencode.cmd") if n == "opencode" else None)
    assert exe.find_executable("opencode", shim_globs=["node_modules/@opencode/cli/bin/opencode.exe"]) == str(real)
    with pytest.raises(exe.AgentNotFound, match="not installed"):
        exe.find_executable("opencode")                                    # nothing to match
    with pytest.raises(exe.AgentNotFound, match="does not exist"):
        exe.find_executable("opencode", override=str(tmp_path / "nope.exe"))


def test_every_agent_is_registered_and_offers_default_permissions():
    assert registry.types() == ("claude", "opencode", "codex")
    for kind in registry.types():
        assert "default" in registry.get(kind).permission_modes


def test_changing_the_agent_resets_a_permission_mode_it_does_not_have():
    base_cfg = profiles.validate({"backend": {"permission_mode": "acceptEdits"}}, profiles.defaults())
    assert profiles.validate({"backend": {"type": "codex"}}, base_cfg)["backend"]["permission_mode"] == "default"
    with pytest.raises(ValueError, match="permission_mode"):
        profiles.validate({"backend": {"type": "codex", "permission_mode": "acceptEdits"}}, base_cfg)
    assert profiles.validate({"backend": {"type": "codex", "permission_mode": "read-only"}}, base_cfg)["backend"]["permission_mode"] == "read-only"


# --- recognising a conversation that no longer fits -------------------------------------------------------------
def test_each_agents_words_for_a_conversation_too_long_for_the_model():
    too_long = ["Prompt is too long", "Internal error: The total text input size exceeds 8 MB",
                "Codex ran out of room in the model's context window. Start a new thread or clear earlier history.",
                "This model's maximum context length is 128000 tokens.", "context_length_exceeded",
                "Request too large for gpt-5", "input is too long for requested model",
                "prompt token count of 210000 exceeds the limit of 200000" ]
    other = ["Rate limit exceeded", "You exceeded your current quota", "Request exceeds the rate limit",
             "the model is unavailable", "Internal error: OpenCode server is unavailable", "", None]
    assert [m for m in too_long if not base.context_overflow(m)] == []
    assert [m for m in other if base.context_overflow(m)] == []
