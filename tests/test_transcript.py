"""Following Claude Code's transcript file: what each line shows, and tailing it as it grows."""
import json

from wireface import transcript
from wireface.transcript import Transcript, entries, find_transcript, transcript_path


def line(**record):
    return json.dumps({"timestamp": "2026-10-06T20:00:00.000Z", "uuid": record.pop("uuid", "u1"), **record}) + "\n"


def test_where_claude_code_keeps_a_session(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert transcript_path(r"C:\projects\ai\faces\data\sandbox", "abc") == \
        tmp_path / "projects" / "C--projects-ai-faces-data-sandbox" / "abc.jsonl"


def test_a_session_resumed_after_its_folder_moved_is_followed_where_it_started(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    old = transcript_path(r"C:\projects\ai-faces\data\sandbox", "s")             # Claude Code goes on writing here
    old.parent.mkdir(parents=True)
    old.write_text(line(type="user", uuid="a", message={"content": "before the move"}), encoding="utf-8")
    new = r"C:\projects\ai\wireface\desktop\data\sandbox"
    transcript_path(new, "s").parent.mkdir(parents=True)                          # it makes the new folder, empty
    assert find_transcript(new, "s") == old
    assert find_transcript(new, "brand-new") == transcript_path(new, "brand-new")  # not written yet: its own folder
    got, resets = [], []
    t = Transcript(lambda: (new, "s"), got.extend, resets.append)
    t.poll()
    assert resets[-1]["exists"] and resets[-1]["path"] == str(old)
    with open(old, "a", encoding="utf-8") as f:
        f.write(line(type="user", uuid="b", message={"content": "after"}))
    t.poll()
    assert [e["text"] for e in got] == ["after"]


def test_what_each_kind_of_line_shows():
    assert [e["kind"] for e in entries(json.loads(line(type="user", message={"role": "user", "content": "fix it"})))] == ["prompt"]
    a = entries(json.loads(line(type="assistant", thinkingDurationMs=3200, message={"role": "assistant", "content": [
        {"type": "thinking", "thinking": ""}, {"type": "text", "text": "Looking."},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}}]})))
    assert [(e["kind"], e["name"]) for e in a] == [("thinking", ""), ("text", ""), ("tool_use", "Bash")]
    assert a[0]["duration_ms"] == 3200 and '"command": "pytest -q"' in a[2]["input"] and a[2]["tool_use_id"] == "t1"
    r = entries(json.loads(line(type="user", message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": [{"type": "text", "text": "2 failed"}]}]})))
    assert r[0]["kind"] == "tool_result" and r[0]["text"] == "2 failed" and r[0]["is_error"] and r[0]["tool_use_id"] == "t1"
    c = entries(json.loads(line(type="attachment", attachment={"type": "environment"},
                                rendered=[{"content": "<system-reminder>\nWorking in X\n</system-reminder>"}])))
    assert c[0]["kind"] == "context" and c[0]["text"] == "Working in X" and c[0]["name"] == "environment"
    assert entries(json.loads(line(type="cost-state", totalCostUSD=0.42)))[0]["text"] == "0.42"
    assert entries(json.loads(line(type="queue-operation", operation="enqueue"))) == []
    side = entries(json.loads(line(type="assistant", isSidechain=True, message={"content": [{"type": "text", "text": "sub"}]})))
    assert side[0]["sidechain"]


def test_long_output_is_cut_with_a_pointer_to_the_file():
    big = "x" * (transcript.TEXT_LIMIT + 5000)
    e = entries(json.loads(line(type="user", message={"content": [{"type": "tool_result", "tool_use_id": "t", "content": big}]})))[0]
    assert len(e["text"]) < len(big) and "5000 more characters in the transcript file" in e["text"]


def test_it_tails_the_session_as_it_grows(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    session = {"id": "s1"}
    got, resets = [], []
    t = Transcript(lambda: (r"C:\work", session["id"]) if session["id"] else None, got.extend, resets.append,
                   lambda: {"sk-secret-123"})
    t.poll()
    assert resets[-1]["available"] and not resets[-1]["exists"]                # no file yet: Claude hasn't written
    path = transcript_path(r"C:\work", "s1")
    path.parent.mkdir(parents=True)
    path.write_text(line(type="user", uuid="a", message={"content": "hello"}), encoding="utf-8")
    t.poll()
    assert [e["text"] for e in got] == ["hello"]
    with open(path, "a", encoding="utf-8") as f:                                  # a line still being written
        f.write(line(type="assistant", uuid="b", message={"content": [{"type": "text", "text": "key sk-secret-123 ok"}]})[:30])
    t.poll()
    assert len(got) == 1
    with open(path, "a", encoding="utf-8") as f:
        f.write(line(type="assistant", uuid="b", message={"content": [{"type": "text", "text": "key sk-secret-123 ok"}]})[30:])
    t.poll()
    assert got[-1]["text"] == "key [hidden] ok" and [e["text"] for e in t.view()["entries"]] == ["hello", "key [hidden] ok"]
    session["id"] = "s2"                                                          # a new conversation
    t.poll()
    assert resets[-1]["session_id"] == "s2" and t.view()["entries"] == []
    session["id"] = None                                                          # another agent, or no session
    t.poll()
    assert not resets[-1]["available"]


def test_an_existing_session_starts_from_its_recent_end(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    path = transcript_path("w", "s")
    path.parent.mkdir(parents=True)
    path.write_text("".join(line(type="user", uuid=f"u{i}", message={"content": f"m{i}"}) for i in range(500)), encoding="utf-8")
    got, resets = [], []
    t = Transcript(lambda: ("w", "s"), got.extend, resets.append)
    t.poll()
    view = resets[-1]
    assert got == [] and view["exists"]                                           # the backlog, not announced as new
    assert [e["text"] for e in t.view()["entries"]][-1] == "m499" and len(t.view()["entries"]) == transcript.BACKLOG
