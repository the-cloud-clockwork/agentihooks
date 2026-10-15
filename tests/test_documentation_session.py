import json

import pytest

from hooks.classifier.errors import ClassifierUnavailable
from hooks.classifier.result import Answer, DecisionResult
from hooks.context import documentation, documentation_session
from tests.test_documentation_evidence import git, repository


class Ledger:
    def __init__(self):
        self.source = {"id": "t1", "title": "Add retry policy", "phase": "p1", "profile": "engineer", "state": "done"}
        self.added = []

    def task(self, slug, task_id):
        return self.source if task_id == "t1" else next((t for t in self.added if t["task"] == task_id), None)

    def add_task(self, slug, fields, by):
        self.added.append(fields)


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    repository(root)
    ledger, jobs = Ledger(), []
    monkeypatch.setattr(documentation_session, "HOME", tmp_path / "state")
    monkeypatch.setattr(documentation_session, "LedgerClient", lambda **kwargs: ledger)
    monkeypatch.setattr(documentation_session, "fork_and_call", lambda fn, *args, **kwargs: jobs.append((fn, args)))
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "fixture")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "t1")
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_CHILD", raising=False)
    payload = {"session_id": "documentation-fixture", "cwd": str(root)}
    documentation_session.start(payload)
    (root / "policy.py").write_text("class WidgetPolicy:\n    retries = 3\n")
    git(root, "add", "policy.py")
    git(root, "commit", "-qm", "Add retry policy")
    return root, payload, ledger, jobs


def answer(needed=0.95, covered=0.05):
    def decide(*args, **kwargs):
        return DecisionResult(
            {
                "needed": Answer("yesno", noul=needed),
                "covered": Answer("yesno", noul=covered),
                "size": Answer("choice", choice="small", confidence=0.9),
            },
            source="fixture",
        )

    return decide


def test_session_end_queues_one_documentation_task_with_source_and_docs_evidence(world, monkeypatch):
    root, payload, ledger, jobs = world
    monkeypatch.setattr(documentation.runner, "decide", answer())
    documentation_session.end(payload)
    assert not ledger.added
    fn, args = jobs.pop()
    fn(*args)
    documentation_session.end(payload)
    for fn, args in jobs:
        fn(*args)
    assert len(ledger.added) == 1
    task = ledger.added[0]
    assert (task["profile"], task["lane"], task["kind"]) == ("documentation", "eng", "code")
    assert task["depends_on"] == ["t1"]
    assert task["phase"] == "p1"
    assert "handbook/policy.md" in task["description"]
    assert "small" in task["description"]
    assert "WidgetPolicy" in task["description"]


@pytest.mark.parametrize(("needed", "covered"), [(0.05, 0.05), (0.95, 0.95)])
def test_no_task_is_queued_for_nonbehavioral_or_already_documented_work(world, monkeypatch, needed, covered):
    _, payload, ledger, jobs = world
    monkeypatch.setattr(documentation.runner, "decide", answer(needed, covered))
    documentation_session.end(payload)
    fn, args = jobs.pop()
    fn(*args)
    assert ledger.added == []


def test_unavailable_classifier_keeps_a_retryable_request(world, monkeypatch):
    _, payload, ledger, jobs = world

    def unavailable(*args, **kwargs):
        raise ClassifierUnavailable("fixture unavailable")

    monkeypatch.setattr(documentation.runner, "decide", unavailable)
    documentation_session.end(payload)
    fn, args = jobs.pop()
    fn(*args)
    assert ledger.added == []
    assert json.loads(args[0].read_text())["status"] == "pending"
    monkeypatch.setattr(documentation.runner, "decide", answer())
    fn(*args)
    assert len(ledger.added) == 1


def test_documentation_tasks_do_not_generate_more_documentation_tasks(world, monkeypatch):
    _, payload, ledger, jobs = world
    ledger.source["profile"] = "documentation"
    monkeypatch.setattr(documentation.runner, "decide", answer())
    documentation_session.end(payload)
    fn, args = jobs.pop()
    fn(*args)
    assert ledger.added == []


def test_classifier_children_schedule_no_documentation_work(world, monkeypatch):
    _, payload, ledger, jobs = world
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_CHILD", "1")
    documentation_session.end(payload)
    assert jobs == []
    assert ledger.added == []


def test_a_handoff_waits_for_delivered_work_before_queueing_documentation(world, monkeypatch):
    _, payload, ledger, jobs = world
    ledger.source["state"] = "claimed"
    monkeypatch.setattr(documentation.runner, "decide", answer())
    documentation_session.end(payload)
    fn, args = jobs.pop()
    fn(*args)
    assert ledger.added == []
    assert json.loads(args[0].read_text())["status"] == "awaiting_delivery"
    ledger.source["state"] = "done"
    fn(*args)
    assert len(ledger.added) == 1


def test_standalone_session_logs_the_verdict_and_prints_a_launch_instruction(tmp_path, monkeypatch, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    repository(root)
    monkeypatch.setattr(documentation_session, "HOME", tmp_path / "state")
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_SWARM_TASK", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_CHILD", raising=False)
    monkeypatch.setattr(documentation.runner, "decide", answer())
    logged = []
    monkeypatch.setattr(documentation_session, "log", lambda event, data: logged.append((event, data)))

    def no_ledger(**kwargs):
        raise AssertionError("standalone session must not create or contact a ledger")

    monkeypatch.setattr(documentation_session, "LedgerClient", no_ledger)
    payload = {"session_id": "standalone-fixture", "cwd": str(root)}
    documentation_session.start(payload)
    (root / "policy.py").write_text("class WidgetPolicy:\n    retries = 3\n")
    git(root, "add", "policy.py")
    git(root, "commit", "-qm", "Add retry policy")
    documentation_session.end(payload)
    output = capsys.readouterr().out
    assert "Add retry policy" in output
    assert "init-agent" in output
    assert any(data.get("needs_documentation") is True for _, data in logged)
