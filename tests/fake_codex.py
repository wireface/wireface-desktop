"""A stand-in for `codex exec --json` for the tests. Arguments: --resume ID (when resuming), then the prompt is on
stdin. Behaviour by prompt: "tool" runs a command and edits a file, "fail" fails the turn, "slow" never finishes,
"warn" prints Codex's config warning, "overflow" runs out of room in the context window, anything else answers
"Hello there." (or "Got the recap." to a prompt that opens with one). Thread "gone" can't be resumed; in thread "long"
every turn runs out of room until it is compacted.

With the argument app-server, it is `codex app-server` (JSON-RPC on stdio), enough to compact a thread: it leaves a
file "compacted-<thread>" in its folder. Thread "too-big" can't be compacted.
"""
import json
import os
import sys
import time

OUT_OF_ROOM = "Codex ran out of room in the model's context window. Start a new thread or clear earlier history before retrying."


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def app_server():
    for line in sys.stdin:
        m = json.loads(line)
        method, rid, p = m.get("method"), m.get("id"), m.get("params") or {}
        if rid is None:
            continue                                      # "initialized"
        if method == "initialize":
            emit({"id": rid, "result": {"userAgent": "fake"}})
        elif method == "thread/resume":
            emit({"id": rid, "result": {"thread": {"id": p["threadId"]}}})
        elif method == "thread/compact/start":
            tid = p["threadId"]
            emit({"id": rid, "result": {}})
            emit({"method": "turn/started", "params": {"threadId": tid, "turn": {"id": "t1", "status": "inProgress"}}})
            if tid == "too-big":
                emit({"method": "turn/completed", "params": {"threadId": tid, "turn": {
                    "id": "t1", "status": "failed", "error": {"message": OUT_OF_ROOM}}}})
                continue
            emit({"method": "item/started", "params": {"threadId": tid, "item": {"type": "contextCompaction", "id": "c1"}}})
            open(f"compacted-{tid}", "w").close()
            emit({"method": "item/completed", "params": {"threadId": tid, "item": {"type": "contextCompaction", "id": "c1"}}})
            emit({"method": "turn/completed", "params": {"threadId": tid, "turn": {"id": "t1", "status": "completed", "error": None}}})
        else:
            emit({"id": rid, "error": {"code": -32601, "message": f"{method} not supported"}})


if sys.argv[1:2] == ["app-server"]:
    app_server()
    sys.exit(0)
resume = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else None
prompt = sys.stdin.read()
if resume == "gone":
    sys.stderr.write("Error: no rollout found for thread id gone\n")
    sys.exit(1)
emit({"type": "thread.started", "thread_id": resume or "thread-1"})
if "warn" in prompt:
    emit({"type": "item.completed", "item": {"id": "w", "type": "error", "message": "Codex is ignoring 3 unrecognized configuration settings."}})
emit({"type": "turn.started"})
if "slow" in prompt:
    emit({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "Working on it."}})
    time.sleep(60)
if "fail" in prompt:
    emit({"type": "turn.failed", "error": {"message": "the model is unavailable"}})
    sys.exit(1)
if "overflow" in prompt or resume in ("long", "too-big") and not os.path.exists(f"compacted-{resume}"):
    emit({"type": "error", "message": OUT_OF_ROOM})
    emit({"type": "turn.failed", "error": {"message": OUT_OF_ROOM}})
    sys.exit(1)
if "tool" in prompt:
    emit({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "I will create the file."}})
    cmd = '"C:\\\\WINDOWS\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe" -Command "Set-Content hello.txt hi"'
    emit({"type": "item.started", "item": {"id": "i2", "type": "command_execution", "command": cmd, "aggregated_output": "", "exit_code": None, "status": "in_progress"}})
    emit({"type": "item.completed", "item": {"id": "i2", "type": "command_execution", "command": cmd, "aggregated_output": "ok", "exit_code": 0, "status": "completed"}})
    emit({"type": "item.started", "item": {"id": "i3", "type": "file_change", "changes": [{"path": "C:\\work\\note.txt", "kind": "add"}], "status": "in_progress"}})
    emit({"type": "item.completed", "item": {"id": "i3", "type": "file_change", "changes": [{"path": "C:\\work\\note.txt", "kind": "add"}], "status": "completed"}})
    emit({"type": "item.started", "item": {"id": "i4", "type": "command_execution", "command": "false", "aggregated_output": "", "exit_code": None, "status": "in_progress"}})
    emit({"type": "item.completed", "item": {"id": "i4", "type": "command_execution", "command": "false", "aggregated_output": "boom", "exit_code": 1, "status": "failed"}})
    emit({"type": "item.completed", "item": {"id": "i5", "type": "todo_list", "items": [{"text": "write", "completed": True}, {"text": "test", "completed": False}]}})
    emit({"type": "item.completed", "item": {"id": "i6", "type": "agent_message", "text": "Done."}})
elif prompt.startswith("(The previous conversation"):
    emit({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "Got the recap."}})
else:
    emit({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "Hello there."}})
emit({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 20, "reasoning_output_tokens": 0}})
