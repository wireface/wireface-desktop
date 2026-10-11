"""A stand-in ACP agent for the tests: speaks the protocol over stdio, with behaviour picked by the prompt text.

  "tool"  -> a tool call that asks permission (and reports which option it was given)
  "slow"  -> starts a reply, then waits for session/cancel
  "retry" -> OpenCode's rate-limit retry notice, then a reply
  "crash" -> exits
  "fail"  -> the conversation is too long for the model, before anything is said
  "broken"-> some other error before anything is said
  "fill"  -> a reply, and the context is 90% full
  "own compaction" -> OpenCode compacting the session on its own, then a reply
  "/compact" -> compacts the session (OpenCode's command, offered after session/new and session/load)
  other   -> "Hello there." (or "Got the recap." for a prompt that opens with one)
A saved session called "gone" can't be loaded; one called "too-big" loads, but every prompt in it fails, /compact too
(as OpenCode's do once the history is over the provider's size limit); in one called "long" prompts fail as too long
until it is compacted.
"""
import json
import sys


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def update(sid, **u):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": u}})


def read():
    line = sys.stdin.readline()
    return json.loads(line) if line else None


def commands(sid):
    update(sid, sessionUpdate="available_commands_update",
           availableCommands=[{"name": "init", "description": "AGENTS.md"}, {"name": "compact", "description": "Compact the session"}])


def error(rid, message):
    send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": message}})


def main():
    n = 100
    compacted = set()
    while True:
        m = read()
        if m is None:
            return
        method, rid, p = m.get("method"), m.get("id"), m.get("params") or {}
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1, "agentInfo": {"name": "Fake", "version": "0"},
                                                          "agentCapabilities": {"loadSession": True}}})
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "fake-1"}})
            commands("fake-1")
        elif method == "session/load":
            if p["sessionId"] == "gone":
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "no such session"}})
            else:
                update(p["sessionId"], sessionUpdate="agent_message_chunk", messageId="old", content={"type": "text", "text": "OLD HISTORY."})
                send({"jsonrpc": "2.0", "id": rid, "result": {}})
                commands(p["sessionId"])
        elif method == "session/prompt":
            sid, text = p["sessionId"], p["prompt"][0]["text"]
            if "crash" in text:
                sys.exit(3)
            if sid == "too-big":
                error(rid, "Internal error: The total text input size exceeds 8 MB")
                continue
            if text == "/compact":
                compaction = {"status": "started", "messageId": "c1", "reason": "manual"}
                update(sid, sessionUpdate="session_info_update", _meta={"opencode/compaction": compaction})
                update(sid, sessionUpdate="session_info_update", _meta={"opencode/compaction": {**compaction, "status": "completed"}})
                compacted.add(sid)
                send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn", "_meta": {}}})
                continue
            if "fail" in text or (sid == "long" and sid not in compacted):
                error(rid, "Internal error: prompt is too long: 70000 tokens > 65536 maximum")
                continue
            if "broken" in text:
                error(rid, "Internal error: something broke")
                continue
            if "own compaction" in text:
                compaction = {"status": "started", "messageId": "c2", "reason": "auto"}
                update(sid, sessionUpdate="session_info_update", _meta={"opencode/compaction": compaction})
                update(sid, sessionUpdate="session_info_update", _meta={"opencode/compaction": {**compaction, "status": "completed"}})
            if "slow" in text:
                update(sid, sessionUpdate="agent_message_chunk", messageId="m1", content={"type": "text", "text": "Working on it. "})
                while True:
                    c = read()
                    if c is None or c.get("method") == "session/cancel":
                        break
                send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "cancelled"}})
                continue
            if "retry" in text:
                update(sid, sessionUpdate="session_info_update", _meta={"opencode/retry": {
                    "attempt": 2, "error": {"message": "limited", "response": {"body": json.dumps({"error": {"metadata": {"raw": "slow down"}}})}}}})
                update(sid, sessionUpdate="session_info_update", _meta={"opencode/retry": None})
            if "tool" in text:
                update(sid, sessionUpdate="agent_thought_chunk", messageId="t", content={"type": "text", "text": "Let me write it."})
                update(sid, sessionUpdate="tool_call", toolCallId="tc1", title="write", kind="edit", status="pending", locations=[], rawInput={})
                update(sid, sessionUpdate="tool_call_update", toolCallId="tc1", status="in_progress", kind="edit", title="write",
                       locations=[{"path": "C:\\work\\hello.txt"}], rawInput={"path": "C:\\work\\hello.txt", "content": "hi"})
                n += 1
                send({"jsonrpc": "2.0", "id": n, "method": "session/request_permission", "params": {
                    "sessionId": sid, "toolCall": {"toolCallId": "tc1", "title": "C:\\work\\hello.txt", "kind": "edit",
                                                   "locations": [{"path": "C:\\work\\hello.txt"}], "rawInput": {"path": "C:\\work\\hello.txt"}},
                    "options": [{"optionId": "once", "name": "Allow once", "kind": "allow_once"},
                                {"optionId": "always", "name": "Always", "kind": "allow_always"},
                                {"optionId": "reject", "name": "Reject", "kind": "reject_once"}]}})
                answer = read()
                outcome = (answer.get("result") or {}).get("outcome") or {}
                chose = outcome.get("optionId") or outcome.get("outcome")
                if chose == "reject":
                    update(sid, sessionUpdate="tool_call_update", toolCallId="tc1", status="failed",
                           content=[{"type": "content", "content": {"type": "text", "text": "permission denied"}}])
                else:
                    update(sid, sessionUpdate="tool_call_update", toolCallId="tc1", status="completed",
                           content=[{"type": "content", "content": {"type": "text", "text": "Created file successfully"}}])
                update(sid, sessionUpdate="agent_message_chunk", messageId="m2", content={"type": "text", "text": f"Chose {chose}. "})
            elif text.startswith("(The previous conversation"):
                update(sid, sessionUpdate="agent_message_chunk", messageId="m1", content={"type": "text", "text": "Got the recap."})
            else:
                update(sid, sessionUpdate="agent_message_chunk", messageId="m1", content={"type": "text", "text": "Hello "})
                update(sid, sessionUpdate="agent_message_chunk", messageId="m1", content={"type": "text", "text": "there."})
            update(sid, sessionUpdate="usage_update", used=90 if "fill" in text else 10, size=100,
                   cost={"amount": 0.002, "currency": "USD"})
            send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn", "usage": {"inputTokens": 7, "outputTokens": 3, "totalTokens": 10}}})
        elif method == "session/cancel":
            pass


main()
