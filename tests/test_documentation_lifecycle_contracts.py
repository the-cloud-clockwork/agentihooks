import json

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
    assert captured[0]["source_task"] == ledger.source
    assert "+class WidgetPolicy:" in captured[0]["changes"]
    assert ledger.added == []


def test_taskless_swarm_request_uses_session_identity_and_summary(world, monkeypatch):
    _, payload, ledger, jobs = world
    monkeypatch.setattr(session.documentation.runner, "decide", answer())
    path = session._path(payload["session_id"])
    row = json.loads(path.read_text())
    row["task"] = ""
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
