import os
import time

from hooks.lifecycle.files import classify_traces
from hooks.lifecycle.model import Root
from hooks.lifecycle.run import sweep

from .conftest import snap

DAY = 86400


def traces(tmp_path, ages):
    folder = tmp_path / "injections"
    folder.mkdir()
    for name, days in ages.items():
        path = folder / f"{name}.jsonl"
        path.write_text("{}\n")
        stamp = time.time() - days * DAY
        os.utime(path, (stamp, stamp))
    return Root("injection-traces", str(folder), "traces", 14)


def names(findings):
    return sorted((os.path.basename(f.path), f.action) for f in findings)


def test_old_ended_traces_are_removed_and_live_or_recent_ones_kept(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_TRACE_KEEP_DAYS", raising=False)
    root = traces(tmp_path, {"ended-old": 20, "live-old": 20, "ended-recent": 3})
    found = classify_traces(root, snap(sessions={42: "live-old"}))
    assert names(found) == [("ended-old.jsonl", "remove")]
    assert classify_traces(root, snap(uptime=5)) == []


def test_the_keep_age_comes_from_the_environment(tmp_path, monkeypatch):
    root = traces(tmp_path, {"a": 3, "b": 1})
    monkeypatch.setenv("AGENTIHOOKS_TRACE_KEEP_DAYS", "2")
    assert names(classify_traces(root, snap())) == [("a.jsonl", "remove")]
    monkeypatch.setenv("AGENTIHOOKS_TRACE_KEEP_DAYS", "0.5")
    assert names(classify_traces(root, snap())) == [("a.jsonl", "remove"), ("b.jsonl", "remove")]


def test_the_sweep_removes_an_old_trace_unless_its_session_came_back(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_TRACE_KEEP_DAYS", raising=False)
    root, home = traces(tmp_path, {"s1": 20}), tmp_path / "state"
    trace = tmp_path / "injections" / "s1.jsonl"
    sweep([root], snap(uptime=10_000), home, act=True, fresh=lambda: snap(uptime=10_000))
    back = lambda: snap(uptime=14_000, sessions={7: "s1"})  # noqa: E731
    report = sweep([root], snap(uptime=14_000), home, act=True, fresh=back)
    assert [f["outcome"] for f in report["findings"]] == ["skipped: no longer safe"]
    assert trace.exists()
    report = sweep([root], snap(uptime=18_000), home, act=True, fresh=lambda: snap(uptime=18_000))
    assert [f["outcome"] for f in report["findings"]] == ["removed"]
    assert not trace.exists()
