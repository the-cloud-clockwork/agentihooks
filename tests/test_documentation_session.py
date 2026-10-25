import fcntl
import json
import os

import pytest

from hooks.classifier.errors import ClassifierUnavailable
from hooks.classifier.result import Answer, DecisionResult
from hooks.context import documentation, documentation_session
from tests.test_documentation_evidence import git, pull, repository


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
    assert task["rank"] == "low"
    assert task["depends_on"] == ["t1"]
    assert task["phase"] == "p1"
    assert "handbook/policy.md" in task["description"]
    assert "small" in task["description"]
    assert "WidgetPolicy" in task["description"]


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
    clock = Clock()
    monkeypatch.setattr(documentation_session, "time", clock)
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
    clock.at += documentation_session.WORKER_OPTIONS["timeout_sec"]
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


class Clock:
    def __init__(self, at=1000.0):
        self.at = at

    def time(self):
        return self.at


def failed_request(world, monkeypatch, decide):
    _, payload, ledger, jobs = world
    clock = Clock()
    monkeypatch.setattr(documentation_session, "time", clock)
    monkeypatch.setattr(documentation.runner, "decide", decide)
    documentation_session.end(payload)
    fn, args = jobs.pop()
    fn(*args)
    return args[0], ledger, clock


def unavailable(*args, **kwargs):
    raise ClassifierUnavailable("fixture unavailable")


def test_a_failed_request_records_its_attempt_and_next_retry_time(world, monkeypatch):
    path, _, clock = failed_request(world, monkeypatch, unavailable)
    row = json.loads(path.read_text())
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["retry_at"] == clock.at + documentation_session.RETRY_DELAYS_S[0]


def test_a_due_request_retries_on_the_timer_and_queues_exactly_one_task(world, monkeypatch):
    path, ledger, clock = failed_request(world, monkeypatch, unavailable)
    monkeypatch.setattr(documentation.runner, "decide", answer())
    documentation_session.retry()
    assert ledger.added == []
    clock.at += documentation_session.RETRY_DELAYS_S[0]
    documentation_session.retry()
    documentation_session.retry()
    assert len(ledger.added) == 1
    row = json.loads(path.read_text())
    assert (row["status"], row["attempts"]) == ("queued", 2)


def test_a_request_past_its_attempt_bound_ends_abandoned_with_a_named_outcome(world, monkeypatch):
    path, ledger, clock = failed_request(world, monkeypatch, answer(0.5))
    calls = []

    def undecided(*args, **kwargs):
        calls.append(1)
        return answer(0.5)(*args, **kwargs)

    monkeypatch.setattr(documentation.runner, "decide", undecided)
    for delay in documentation_session.RETRY_DELAYS_S:
        clock.at += delay
        documentation_session.retry()
    row = json.loads(path.read_text())
    assert row["status"] == "abandoned"
    assert row["outcome"] == (
        f"gave up after {len(documentation_session.RETRY_DELAYS_S)} attempts: classifier undecided"
    )
    assert len(calls) == len(documentation_session.RETRY_DELAYS_S) - 1
    clock.at += documentation_session.RETRY_DELAYS_S[-1]
    documentation_session.retry()
    assert len(calls) == len(documentation_session.RETRY_DELAYS_S) - 1
    assert ledger.added == []


def test_an_abandoned_request_is_not_dispatched_again_at_session_end(world, monkeypatch):
    path, _, clock = failed_request(world, monkeypatch, answer(0.5))
    for delay in documentation_session.RETRY_DELAYS_S:
        clock.at += delay
        documentation_session.retry()
    _, payload, _, jobs = world
    documentation_session.end(payload)
    assert jobs == []
    assert "abandoned" in documentation_session.TERMINAL


def test_a_failed_worker_records_the_error_as_the_retry_reason(world, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("fixture")

    monkeypatch.setattr(documentation_session.documentation_evidence, "collect", broken)
    path, _, _ = failed_request(world, monkeypatch, answer())
    row = json.loads(path.read_text())
    assert (row["status"], row["attempts"], row["reason"]) == ("pending", 1, "RuntimeError")


def test_a_session_end_leaves_its_own_worker_the_first_attempt(world, monkeypatch):
    _, payload, ledger, jobs = world
    clock = Clock()
    monkeypatch.setattr(documentation_session, "time", clock)
    monkeypatch.setattr(documentation.runner, "decide", answer())
    documentation_session.end(payload)
    documentation_session.retry()
    assert ledger.added == []
    clock.at += documentation_session.WORKER_OPTIONS["timeout_sec"]
    documentation_session.retry()
    assert len(ledger.added) == 1
    assert jobs[0][0] is documentation_session.process_pending


def test_a_retry_pass_is_skipped_while_another_holds_the_retry_lock(world, monkeypatch):
    path, ledger, clock = failed_request(world, monkeypatch, unavailable)
    monkeypatch.setattr(documentation.runner, "decide", answer())
    clock.at += documentation_session.RETRY_DELAYS_S[0]
    with (path.parent / documentation_session.RETRY_LOCK).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        documentation_session.retry()
    assert ledger.added == []
    documentation_session.retry()
    assert len(ledger.added) == 1


def test_the_retry_timer_dispatches_a_detached_worker_only_when_requests_exist(tmp_path, monkeypatch):
    jobs = []
    monkeypatch.setattr(documentation_session, "HOME", tmp_path)
    monkeypatch.setattr(documentation_session, "fork_and_call", lambda fn, **kwargs: jobs.append((fn, kwargs)))
    documentation_session.schedule_retry()
    assert jobs == []
    (tmp_path / "documentation").mkdir()
    documentation_session.schedule_retry()
    assert jobs == [(documentation_session.retry, {"timeout_sec": 180, "task_name": "documentation-retry"})]


def test_retries_are_spaced_from_five_minutes_to_two_hours_behind_a_named_lock():
    assert documentation_session.RETRY_DELAYS_S == (300, 900, 1800, 3600, 7200)
    assert documentation_session.RETRY_LOCK == "retry.lock"


def test_an_exhausted_request_without_a_reason_is_abandoned_as_unknown_and_logged(tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(documentation_session, "log", lambda event, data: logged.append((event, data)))
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"session": "s", "swarm": "", "status": "pending", "attempts": 5}))
    documentation_session.process(path, 0.0)
    row = json.loads(path.read_text())
    assert (row["status"], row["outcome"]) == ("abandoned", "gave up after 5 attempts: unknown")
    assert logged == [("documentation request abandoned", {"outcome": row["outcome"], "session": "s"})]


def stored(path, **row):
    path.write_text(json.dumps({"session": path.stem, "swarm": "fixture", "status": "pending", **row}))
    return path


def test_process_leaves_finished_and_not_yet_due_requests_untouched(tmp_path, monkeypatch):
    finished = []
    monkeypatch.setattr(documentation_session, "_finish", lambda path, row: finished.append(path))
    done = stored(tmp_path / "done.json", status="complete")
    early = stored(tmp_path / "early.json", retry_at=100.0)
    before = (done.read_text(), early.read_text())
    documentation_session.process(done)
    documentation_session.process(early, 50.0)
    assert (done.read_text(), early.read_text()) == before
    assert finished == []


def test_process_records_the_attempt_before_the_worker_runs(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(documentation_session, "time", Clock(1000.0))
    monkeypatch.setattr(documentation_session, "_finish", lambda path, row: seen.append(json.loads(path.read_text())))
    documentation_session.process(stored(tmp_path / "request.json"), 1000.0)
    assert [(row["attempts"], row["retry_at"]) for row in seen] == [
        (1, 1000.0 + documentation_session.RETRY_DELAYS_S[0])
    ]


def test_retry_pending_runs_due_requests_oldest_write_first(tmp_path, monkeypatch):
    processed = []
    monkeypatch.setattr(documentation_session, "HOME", tmp_path)
    monkeypatch.setattr(documentation_session, "time", Clock(0.0))
    monkeypatch.setattr(documentation_session, "process", lambda path, now: processed.append((path.name, now)))
    folder = tmp_path / "documentation"
    folder.mkdir()
    older = stored(folder / "b.json")
    stored(folder / "a.json")
    stored(folder / "later.json", retry_at=10.0)
    os.utime(older, ns=(1, 1))
    documentation_session.retry_pending()
    assert processed == [("b.json", 0.0), ("a.json", 0.0)]
