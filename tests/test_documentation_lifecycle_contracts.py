import json
import types

import pytest

from hooks.context import documentation_session as session
from tests.test_documentation_session import answer
from tests.test_documentation_session import world as world


def test_session_end_records_a_verdict_and_detaches_with_a_bounded_worker(world, monkeypatch):
    _, payload, _, _ = world
    monkeypatch.setattr(session.documentation.runner, "decide", answer())
    jobs, events = [], []
    monkeypatch.setattr(session, "fork_and_call", lambda *args, **kwargs: jobs.append((args, kwargs)))
    monkeypatch.setattr(session, "log", lambda *args: events.append(args))
    session.end(payload)
    args, options = jobs.pop()
    assert options == {"timeout_sec": 180, "task_name": "documentation-session-end"}
    args[0](*args[1:])
    row = json.loads(args[1].read_text())
    assert events == [("documentation verdict", {"session": payload["session_id"], **row["verdict"]})]


def test_failed_dispatch_and_failed_worker_have_distinct_retry_records(world, monkeypatch):
    _, payload, _, _ = world
    events = []
    monkeypatch.setattr(session, "log", lambda *args: events.append(args))

    def offline(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(session, "fork_and_call", offline)
    session.end(payload)
    assert events == [("documentation dispatch unavailable", {"error": "OSError"})]
    monkeypatch.setattr(session, "_finish", offline)
    path = session._path(payload["session_id"])
    session.process(path)
    assert events[-1] == (
        "documentation request retained",
        {"error": "OSError", "session": payload["session_id"]},
    )
    row = json.loads(path.read_text())
    assert (row["status"], row["error"]) == ("pending", "OSError")


def test_uncertain_verdict_is_persisted_for_retry_with_full_source_context(world, monkeypatch):
    _, payload, ledger, jobs = world
    captured = []

    def classify(evidence):
        captured.append(evidence)
        return {"needs_documentation": None, "size": "medium", "source": "uncertain"}

    monkeypatch.setattr(session.documentation, "classify", classify)
    session.end(payload)
    fn, args = jobs.pop()
    fn(*args)
    row = json.loads(args[0].read_text())
    assert row["verdict"] == {"needs_documentation": None, "size": "medium", "source": "uncertain"}
    assert row["status"] == "pending"
    assert "error" not in row
    assert captured[0]["source_task"] == ledger.source
    assert "+class WidgetPolicy:" in captured[0]["changes"]
    assert ledger.added == []


def test_taskless_swarm_request_uses_session_identity_and_summary(world, monkeypatch):
    _, payload, ledger, jobs = world
    monkeypatch.setattr(session.documentation.runner, "decide", answer())
    path = session._path(payload["session_id"])
    row = json.loads(path.read_text())
    row["task"] = ""
    original_lookup = ledger.task

    def lookup(slug, task_id):
        assert task_id
        return original_lookup(slug, task_id)

    monkeypatch.setattr(ledger, "task", lookup)
    session._write(path, row)
    session.end(payload)
    fn, args = jobs.pop()
    fn(*args)
    first = ledger.added[0]
    assert first["title"] == "Document Add retry policy"
    assert first["phase"] == ""
    assert first["depends_on"] == []
    assert "Document the delivered behavior of Add retry policy.\n" in first["description"]
    assert "Delivered pull request: .\n" in first["description"]
    assert "Source requirements: \n" in first["description"]
    row.update(session="different-session", status="pending")
    session._write(path, row)
    fn(*args)
    assert len(ledger.added) == 2
    assert ledger.added[1]["task"] != first["task"]


def test_unchanged_standalone_verdict_has_an_explicit_reason(world, monkeypatch):
    _, payload, _, jobs = world
    path = session._path(payload["session_id"])
    row = json.loads(path.read_text())
    row.update(swarm="", task="")
    row["base"] = session.documentation_evidence.git(row["root"], "rev-parse", "HEAD")
    session._write(path, row)
    session.end(payload)
    fn, args = jobs.pop()
    fn(*args)
    result = json.loads(path.read_text())
    assert result["status"] == "complete"
    assert result["verdict"] == {"needs_documentation": False, "size": "none", "source": "no changes"}


def test_notice_scan_skips_unrelated_shown_and_empty_records(world, monkeypatch):
    root, payload, _, _ = world
    folder = session._path(payload["session_id"]).parent
    notices = []
    monkeypatch.setattr(session, "inject_context", notices.append)
    locks = []
    original_flock = session.fcntl.flock

    def lock(fd, operation):
        locks.append(operation)
        original_flock(fd, operation)

    monkeypatch.setattr(session.fcntl, "flock", lock)
    rows = [
        {"repository": str(root), "shown": True, "notice": "already shown"},
        {"repository": str(root)},
        {"repository": "elsewhere", "notice": "other repository"},
        {"repository": str(root), "notice": "document this repository"},
    ]
    for index, row in enumerate(rows):
        (folder / f"{index}.json").write_text(json.dumps(row))
    session._show_notice(root)
    session._show_notice(root)
    assert notices == ["document this repository"]
    assert len(locks) == 1


@pytest.mark.parametrize("update", [{"shown": True}, {"notice": ""}])
def test_notice_is_rechecked_after_acquiring_the_lock(world, monkeypatch, update):
    root, payload, _, _ = world
    path = session._path(payload["session_id"])
    row = {"repository": str(root), "notice": "document this repository"}
    path.write_text(json.dumps(row))
    notices = []
    monkeypatch.setattr(session, "inject_context", notices.append)

    def delivered_elsewhere(*args):
        row.update(update)
        path.write_text(json.dumps(row))

    monkeypatch.setattr(session.fcntl, "flock", delivered_elsewhere)
    session._show_notice(root)
    assert notices == []


def test_worker_opens_a_service_ledger_only_for_swarm_requests(world, monkeypatch):
    _, payload, ledger, jobs = world
    options = []

    def client(**kwargs):
        options.append(kwargs)
        return ledger

    monkeypatch.setattr(session, "LedgerClient", client)
    monkeypatch.setattr(session.documentation.runner, "decide", answer())
    session.end(payload)
    fn, args = jobs.pop()
    fn(*args)
    assert options == [{"service": True}]


def test_standalone_snapshots_use_empty_swarm_and_task_identifiers(world, monkeypatch):
    root, _, _, _ = world
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_SWARM_TASK", raising=False)
    payload = {"session_id": "standalone-schema", "cwd": str(root)}
    session.start(payload)
    row = json.loads(session._path(payload["session_id"]).read_text())
    assert row["swarm"] == ""
    assert row["task"] == ""


def test_retry_batch_processes_exactly_three_eligible_requests_even_when_current_is_first(world, monkeypatch):
    import os

    root, payload, _, _ = world
    folder = session._path(payload["session_id"]).parent
    current = folder / "0-current.json"
    current.write_text(json.dumps({"swarm": "fixture", "status": "pending"}))
    os.utime(current, (1, 1))
    expected = []
    for index in range(4):
        path = folder / f"request-{index}.json"
        path.write_text(json.dumps({"swarm": "fixture", "status": "pending"}))
        os.utime(path, (index + 2, index + 2))
        expected.append(path)
    for name, swarm, status in (("active", "fixture", "active"), ("foreign", "another", "pending")):
        path = folder / f"{name}.json"
        path.write_text(json.dumps({"swarm": swarm, "status": status}))
        os.utime(path, (1.5, 1.5))
    calls = []
    monkeypatch.setattr(session, "time", types.SimpleNamespace(time=lambda: 50.0))
    monkeypatch.setattr(session, "process", lambda path, now=None: calls.append((path, now)))
    session.process_pending(current)
    assert calls == [(current, None), *((path, 50.0) for path in expected[:3])]
