import json
import os

import pytest

from hooks.classifier.errors import ClassifierUnavailable
from hooks.classifier.result import Answer, DecisionResult
from hooks.context import documentation, documentation_session
from tests.test_documentation_evidence import git, pull, repository, unrelated


class Ledger:
    def __init__(self):
        self.source = {
            "id": "t1",
            "title": "Add retry policy",
            "phase": "p1",
            "profile": "engineer",
            "state": "done",
            "pr_url": "https://example.test/pull/7",
        }
        self.added = []

    def task(self, slug, task_id):
        return self.source if task_id == "t1" else next((t for t in self.added if t["task"] == task_id), None)

    def add_task(self, slug, fields, by):
        self.added.append(fields)


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    baseline = repository(root)
    ledger, jobs = Ledger(), []
    monkeypatch.setattr(
        documentation_session.documentation_evidence,
        "pull_request_diff",
        lambda url: git(root, "diff", baseline, "HEAD", "--", "policy.py"),
    )
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
    unrelated(root)
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
    assert task["rank"] == "low"
    assert task["depends_on"] == ["t1"]
    assert task["phase"] == "p1"
    assert "handbook/policy.md" in task["description"]
    assert "small" in task["description"]
    assert "WidgetPolicy" in task["description"]
    assert "LoginRecovery" not in task["description"]


def test_a_source_task_without_a_pull_request_queues_documentation_with_no_diff_and_says_so(world, monkeypatch):
    root, payload, ledger, jobs = world
    del ledger.source["pr_url"]
    monkeypatch.setattr(documentation.runner, "decide", answer())
    (root / "login.py").write_text("class LoginRecovery:\n    attempts = 2\n")
    git(root, "add", "login.py")
    git(root, "commit", "-qm", "Recover the master login")
    documentation_session.end(payload)
    fn, args = jobs.pop()
    fn(*args)
    description = ledger.added[0]["description"]
    assert "Delivered pull request: none recorded on the source task, so no delivered diff is attached." in description
    assert "LoginRecovery" not in description
    assert "+class WidgetPolicy:" not in description


@pytest.mark.parametrize("task", ["master", "t9"])
def test_a_session_named_for_no_ledger_task_queues_no_documentation_task(world, monkeypatch, task):
    root, _, ledger, jobs = world
    monkeypatch.setattr(documentation.runner, "decide", answer())
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", task)
    payload = {"session_id": f"documentation-{task}", "cwd": str(root)}
    documentation_session.start(payload)
    (root / "limits.py").write_text("class WidgetLimits:\n    ceiling = 5\n")
    git(root, "add", "limits.py")
    git(root, "commit", "-qm", "Add widget limits")
    documentation_session.end(payload)
    fn, args = jobs.pop()
    fn(*args)
    assert ledger.added == []
    row = json.loads(args[0].read_text())
    assert row["status"] == "complete"
    assert row["verdict"] == {"needs_documentation": False, "size": "none", "source": "no ledger task"}


def test_a_swarm_session_without_a_task_queues_documentation_without_a_dependency(world, monkeypatch):
    root, _, ledger, jobs = world
    monkeypatch.setattr(documentation.runner, "decide", answer())
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "")
    payload = {"session_id": "documentation-untasked", "cwd": str(root)}
    documentation_session.start(payload)
    (root / "limits.py").write_text("class WidgetLimits:\n    ceiling = 5\n")
    git(root, "add", "limits.py")
    git(root, "commit", "-qm", "Add widget limits")
    documentation_session.end(payload)
    fn, args = jobs.pop()
    fn(*args)
    assert len(ledger.added) == 1
    assert ledger.added[0]["depends_on"] == []


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


def test_a_closing_agent_queues_documentation_with_its_source_dependency(world, monkeypatch):
    _, payload, ledger, jobs = world
    ledger.source["state"] = "pr"
    monkeypatch.setattr(documentation.runner, "decide", answer())
    documentation_session.end(payload)
    fn, args = jobs.pop()
    fn(*args)
    assert len(ledger.added) == 1
    assert ledger.added[0]["depends_on"] == ["t1"]
    assert json.loads(args[0].read_text())["status"] == "queued"


def test_standalone_completion_detaches_and_shows_one_notice_in_the_same_repository(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    repository(root)
    monkeypatch.setattr(documentation_session, "HOME", tmp_path / "state")
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_SWARM_TASK", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_CHILD", raising=False)
    monkeypatch.setattr(documentation.runner, "decide", answer())
    logged, notices, jobs = [], [], []
    monkeypatch.setattr(documentation_session, "log", lambda event, data: logged.append((event, data)))
    monkeypatch.setattr(documentation_session, "inject_context", notices.append)
    monkeypatch.setattr(documentation_session, "fork_and_call", lambda fn, *args, **kwargs: jobs.append((fn, args)))

    def no_ledger(**kwargs):
        raise AssertionError("standalone session must not create or contact a ledger")

    monkeypatch.setattr(documentation_session, "LedgerClient", no_ledger)
    payload = {"session_id": "standalone-fixture", "cwd": str(root)}
    documentation_session.start(payload)
    (root / "policy.py").write_text("class WidgetPolicy:\n    retries = 3\n")
    git(root, "add", "policy.py")
    git(root, "commit", "-qm", "Add retry policy")
    documentation_session.end(payload)
    assert len(jobs) == 1
    assert not logged
    fn, args = jobs.pop()
    fn(*args)
    assert any(data.get("needs_documentation") is True for _, data in logged)
    assert notices == []
    other = tmp_path / "other"
    other.mkdir()
    repository(other)
    documentation_session.start({"session_id": "other-repository", "cwd": str(other)})
    assert notices == []
    documentation_session.start({"session_id": "next-session", "cwd": str(root)})
    documentation_session.start({"session_id": "later-session", "cwd": str(root)})
    assert len(notices) == 1
    assert "Add retry policy" in notices[0]
    assert "init-agent" in notices[0]
    assert len(notices[0]) < 300


def test_an_unchanged_session_records_no_documentation_need(tmp_path, monkeypatch):
    repository(tmp_path)
    monkeypatch.setattr(documentation_session, "HOME", tmp_path / "state")
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_CHILD", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("unchanged work needs no model call")

    monkeypatch.setattr(documentation.runner, "decide", forbidden)
    monkeypatch.setattr(documentation_session, "fork_and_call", lambda fn, *args, **kwargs: fn(*args))
    payload = {"session_id": "unchanged-fixture", "cwd": str(tmp_path)}
    documentation_session.start(payload)
    documentation_session.end(payload)
    records = list((documentation_session.HOME / "documentation").glob("*.json"))
    assert len(records) == 1
    row = json.loads(records[0].read_text())
    assert row["status"] == "complete"
    assert row["verdict"]["needs_documentation"] is False


def test_pending_retries_rotate_past_three_retained_requests(world, monkeypatch):
    root, payload, ledger, jobs = world
    monkeypatch.setattr(documentation.runner, "decide", answer(0.05))
    documentation_session.end(payload)
    fn, args = jobs.pop()
    fn(*args)
    for index in range(4):
        retained = {"session_id": f"retained-{index}", "cwd": str(root)}
        documentation_session.start(retained)
        documentation_session.end(retained)
    pending = sorted(path for path in args[0].parent.glob("*.json") if path != args[0])
    for index, path in enumerate(pending, 1):
        os.utime(path, ns=(index, index))
    decisions = iter([answer(0.5), answer(0.5), answer(0.5), answer()])

    def decide(*args, **kwargs):
        return next(decisions, answer(0.5))(*args, **kwargs)

    monkeypatch.setattr(documentation.runner, "decide", decide)
    fn(*args)
    assert ledger.added == []
    fn(*args)
    assert json.loads(pending[3].read_text())["status"] == "queued"
    assert len(ledger.added) == 1


def test_retry_batch_does_not_classify_another_session_before_it_ends(world, monkeypatch):
    root, payload, ledger, jobs = world
    documentation_session.start({"session_id": "still-working", "cwd": str(root)})
    calls = []

    def decide(state, *args, **kwargs):
        calls.append(state)
        return answer(0.05)(state, *args, **kwargs)

    monkeypatch.setattr(documentation.runner, "decide", decide)
    documentation_session.end(payload)
    fn, args = jobs.pop()
    fn(*args)
    assert len(calls) == 1
    assert ledger.added == []


def test_an_interrupted_worker_advances_the_pending_request_retry_order(world, monkeypatch):
    _, payload, _, jobs = world

    class Interrupted(BaseException):
        pass

    def interrupt(*args, **kwargs):
        raise Interrupted()

    monkeypatch.setattr(documentation.runner, "decide", interrupt)
    documentation_session.end(payload)
    fn, args = jobs.pop()
    os.utime(args[0], ns=(1, 1))
    with pytest.raises(Interrupted):
        fn(*args)
    assert args[0].stat().st_mtime_ns > 1


def test_commits_pulled_from_dev_are_not_delivered_by_the_session(tmp_path, monkeypatch):
    repository(tmp_path)
    monkeypatch.setattr(documentation_session, "HOME", tmp_path / "state")
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_CHILD", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("pulled work needs no model call")

    monkeypatch.setattr(documentation.runner, "decide", forbidden)
    monkeypatch.setattr(documentation_session, "fork_and_call", lambda fn, *args, **kwargs: fn(*args))
    payload = {"session_id": "fast-forward-fixture", "cwd": str(tmp_path)}
    documentation_session.start(payload)
    pull(tmp_path, "policy.py", "class WidgetPolicy:\n    retries = 3\n")
    documentation_session.end(payload)
    row = json.loads(next((documentation_session.HOME / "documentation").glob("*.json")).read_text())
    assert row["status"] == "complete"
    assert row["verdict"]["needs_documentation"] is False
    assert "notice" not in row
