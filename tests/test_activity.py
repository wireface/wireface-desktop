import json

import pytest

from wireface.activity import ActivityLog, clip


def test_events_are_updated_in_place():
    log = ActivityLog()
    seen = []
    log.listeners.append(lambda ev: seen.append((ev.id, ev.status)))
    ev = log.emit("tool", id="t1", name="Read", summary="app.py", status="running")
    log.update("t1", status="ok", output="contents")
    assert [e["id"] for e in log.recent()] == ["t1"]
    assert log.get("t1").output == "contents" and log.get("t1").duration_ms is not None
    assert seen == [("t1", "running"), ("t1", "ok")] and ev is log.get("t1")


def test_unknown_things_are_refused():
    log = ActivityLog()
    with pytest.raises(ValueError):
        log.emit("bogus")
    log.emit("notice", id="n")
    with pytest.raises(ValueError):
        log.update("n", colour="red")
    with pytest.raises(ValueError):
        log.update("n", status="finished")
    assert log.update("missing", status="ok") is None


def test_long_text_is_clipped_and_says_so():
    assert clip("x" * 10, 20) == "x" * 10
    out = clip("x" * 100, 20)
    assert out.startswith("x" * 20) and "80 more characters" in out
    log = ActivityLog()
    ev = log.emit("tool", name="Bash", output="y" * 50_000)
    assert len(ev.output) < 9_000
    reply = log.emit("assistant", output="z" * 15_000)
    assert len(reply.output) == 15_000                                  # replies get a larger allowance


def test_the_ring_forgets_the_oldest():
    log = ActivityLog(limit=3)
    for i in range(5):
        log.emit("notice", id=f"n{i}")
    assert [e["id"] for e in log.recent()] == ["n2", "n3", "n4"]


def test_finished_events_are_written_to_a_daily_file(tmp_path):
    log = ActivityLog(tmp_path)
    log.emit("tool", id="t", status="running")
    assert not list(tmp_path.glob("*.jsonl"))                           # only finished events are stored
    log.update("t", status="ok", output="done")
    (file,) = tmp_path.glob("activity-*.jsonl")
    row = json.loads(file.read_text().splitlines()[0])
    assert row["id"] == "t" and row["output"] == "done"


def test_secrets_never_reach_the_log(tmp_path):
    log = ActivityLog(tmp_path)
    log.secrets.add("sk-very-secret")
    ev = log.emit("tool", name="Bash", input="curl -H 'xi-api-key: sk-very-secret'", output="ok sk-very-secret")
    assert "sk-very-secret" not in ev.input and "sk-very-secret" not in ev.output
    assert "sk-very-secret" not in json.dumps(log.recent())


def test_finish_running_closes_open_steps_of_a_turn():
    log = ActivityLog()
    log.emit("turn", id="turn", status="running")
    log.emit("tool", id="a", turn_id="turn", status="running")
    log.emit("assistant", id="b", turn_id="turn", status="running", output="hi")
    log.emit("task", id="bg", turn_id="turn", status="running")         # background work outlives the turn
    log.finish_running("turn", "(interrupted)")
    assert log.get("a").status == "error" and log.get("a").output == "(interrupted)"
    assert log.get("b").status == "ok"
    assert log.get("bg").status == "running" and log.get("turn").status == "running"


def test_history_comes_back_after_a_restart(tmp_path):
    first = ActivityLog(tmp_path)
    first.emit("turn", id="t1", summary="old request", status="ok")
    first.emit("tool", id="a", turn_id="t1", name="Read", status="ok", output="v1")
    first.update("a", output="v2")                                       # written twice: the later one wins
    first.emit("tool", id="running", turn_id="t1", status="running")     # never stored (it had not finished)
    second = ActivityLog(tmp_path)
    assert second.load_history() == 2
    assert [e["id"] for e in second.recent()] == ["t1", "a"] and second.get("a").output == "v2"
    seen = []
    second.listeners.append(seen.append)
    assert second.load_history() == 2 and seen == []                     # not announced, not duplicated


def test_history_of_nothing_is_nothing(tmp_path):
    assert ActivityLog(tmp_path / "missing").load_history() == 0
