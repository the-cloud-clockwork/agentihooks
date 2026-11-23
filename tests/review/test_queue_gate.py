"""The review gate at swarm merge queue: an independent review of the current head whose findings the author read."""

from collections.abc import Callable
from typing import Any

import pytest

from scripts.gates import catalog, enforcement, log, modes
from scripts.review import gate, record
from scripts.swarm import cli, merge_queue, queue_exit
from scripts.swarm.store import RedisStore
from tests.review.fakes import AUTHOR, HEAD, REVIEWER, SLUG, URL, FakeForge, swarm

pytestmark = pytest.mark.unit
UNREAD = (
    f"1 review findings on head {HEAD[:12]} of {URL} are unread: f1; "
    f"read each and mark it with agentihooks review read {URL} --slug {SLUG}"
)
MISSING = (
    f"no independent review on head {HEAD[:12]} of {URL}: a reviewer other than {AUTHOR} "
    "posts its findings with agentihooks review post before this pull request queues"
)


def _queue(monkeypatch: pytest.MonkeyPatch, found: RedisStore) -> tuple[int, list[str], list[log.Row]]:
    monkeypatch.setattr(cli, "connect", lambda: found)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", AUTHOR)
    monkeypatch.setattr(cli, "_queue_intent", lambda store, args, agent, head: None)
    monkeypatch.setattr(cli, "_require_target", lambda *args: None)
    monkeypatch.setattr(queue_exit, "require_fresh", lambda url, head: None)
    rows: list[log.Row] = []
    monkeypatch.setattr(log, "append", lambda slug, row, home=None: rows.append(row))
    queued: list[str] = []

    def operate(action: str, url: str, admit: Callable[[str], Any], **kwargs: Any) -> dict[str, Any]:
        admit(HEAD)
        queued.append(url)
        return {"queued": True}

    monkeypatch.setattr(merge_queue, "operate", operate)
    return cli.main([SLUG, "merge", "queue", URL]), queued, rows


def _rows(rows: list[log.Row]) -> list[tuple[str, str, str, str, str, str]]:
    return [(row.gate, row.kind, row.agent, row.task, row.tool, row.reason) for row in rows]


@pytest.mark.feature("review-findings-queue-gate")
def test_the_review_gate_defaults_to_log_only_and_takes_an_enforce_switch() -> None:
    assert enforcement.ENFORCEMENTS["review"] is enforcement.REVIEW
    assert catalog.defaults()["review"] == "observe"
    assert modes.configured(enforcement.REVIEW, {}) == "observe"
    assert modes.configured(enforcement.REVIEW, {"review": "enforce"}) == "enforce"


@pytest.mark.feature("review-findings-queue-gate")
def test_an_unread_review_logs_a_would_be_refusal_and_queues(monkeypatch: pytest.MonkeyPatch) -> None:
    found = swarm({})
    record.post(found, SLUG, FakeForge(), URL, REVIEWER, "r1", ["First"])
    code, queued, rows = _queue(monkeypatch, found)
    assert (code, queued) == (0, [URL])
    assert _rows(rows) == [("review", "observe", AUTHOR, "t1", "queue", UNREAD)]


@pytest.mark.feature("review-findings-queue-gate")
def test_an_enforced_gate_refuses_an_unread_review(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    found = swarm({"review": "enforce"})
    record.post(found, SLUG, FakeForge(), URL, REVIEWER, "r1", ["First"])
    code, queued, rows = _queue(monkeypatch, found)
    assert (code, queued) == (1, [])
    assert _rows(rows) == [("review", "deny", AUTHOR, "t1", "queue", UNREAD)]
    assert UNREAD in capsys.readouterr().err


@pytest.mark.feature("review-findings-queue-gate")
@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_a_review_the_author_read_queues_with_no_gate_row(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    found, forge = swarm({"review": mode}), FakeForge()
    record.post(found, SLUG, forge, URL, REVIEWER, "r1", ["First"])
    record.mark(found, SLUG, forge, URL, AUTHOR, ["f1"], "")
    code, queued, rows = _queue(monkeypatch, found)
    assert (code, queued, rows) == (0, [URL], [])


@pytest.mark.feature("review-findings-queue-gate")
def test_another_agents_read_does_not_satisfy_the_author(monkeypatch: pytest.MonkeyPatch) -> None:
    found, forge = swarm({"review": "enforce"}), FakeForge()
    record.post(found, SLUG, forge, URL, REVIEWER, "r1", ["First"])
    record.mark(found, SLUG, forge, URL, "engineer@a1b2c3-0003", ["f1"], "")
    code, queued, rows = _queue(monkeypatch, found)
    assert code == 1
    assert queued == []
    assert len(rows) == 1
    assert "unread" in rows[0].reason
    record.mark(found, SLUG, forge, URL, AUTHOR, ["f1"], "")
    assert _queue(monkeypatch, found) == (0, [URL], [])
    record.mark(found, SLUG, forge, URL, "engineer@a1b2c3-0003", ["f1"], "")
    assert _queue(monkeypatch, found) == (0, [URL], [])


@pytest.mark.feature("review-findings-queue-gate")
@pytest.mark.parametrize(("by", "task"), [(AUTHOR, "r1"), (REVIEWER, "t1")])
def test_a_review_posted_by_the_author_or_its_task_is_not_independent(
    monkeypatch: pytest.MonkeyPatch, by: str, task: str
) -> None:
    found, forge = swarm({"review": "enforce"}), FakeForge()
    record.post(found, SLUG, forge, URL, by, task, ["Self review"])
    record.mark(found, SLUG, forge, URL, "reviewer@a1b2c3-0009", [], "")
    code, queued, rows = _queue(monkeypatch, found)
    assert (code, queued) == (1, [])
    assert _rows(rows) == [("review", "deny", AUTHOR, "t1", "queue", MISSING)]


@pytest.mark.feature("review-findings-queue-gate")
def test_a_head_without_a_review_logs_a_would_be_refusal_and_queues(monkeypatch: pytest.MonkeyPatch) -> None:
    code, queued, rows = _queue(monkeypatch, swarm({}))
    assert (code, queued) == (0, [URL])
    assert _rows(rows) == [("review", "observe", AUTHOR, "t1", "queue", MISSING)]


@pytest.mark.feature("review-findings-queue-gate")
def test_a_skipped_review_gate_neither_logs_nor_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    code, queued, rows = _queue(monkeypatch, swarm({"review": "off"}))
    assert (code, queued, rows) == (0, [URL], [])


@pytest.mark.feature("review-findings-queue-gate")
def test_refusal_names_the_author_task_full_head_and_all_unread_findings() -> None:
    from scripts.gates.base import Who

    head = "a" * 40
    author = Who(name=AUTHOR, swarm=SLUG, task="t1")
    assert gate.refusal(None, SLUG, URL, head, Who(task="t1")) == (
        f"no author recorded for task t1 on head {head[:12]} of {URL}"
    )
    assert gate.refusal(None, SLUG, URL, head, author) == (
        f"no independent review on head {head[:12]} of {URL}: a reviewer other than {AUTHOR} "
        "posts its findings with agentihooks review post before this pull request queues"
    )
    findings = tuple(record.Finding(f"f{i}", "First", URL, REVIEWER, "r1") for i in (1, 2))
    assert gate.refusal(record.Record(URL, head, findings), SLUG, URL, head, author) == (
        f"2 review findings on head {head[:12]} of {URL} are unread: f1, f2; "
        f"read each and mark it with agentihooks review read {URL} --slug {SLUG}"
    )
