from importlib import import_module
from typing import Any

import pytest

from scripts.gates import catalog, enforcement, log
from scripts.gates.base import Who
from scripts.review.record import Finding, Record
from tests.review.fakes import HEAD, REVIEWER, SLUG, URL

AUTHOR = "engineer@a1b2c3-0001"
LONG_HEAD = "0123456789abcdef0123456789abcdef01234567"
UNREAD = (
    f"1 review findings on head 0123456789ab of {URL} are unread: f1; "
    f"read each and mark it with agentihooks review read {URL} --slug {SLUG}"
)


@pytest.mark.feature("review-queue-gate")
def test_review_defaults_to_log_only_and_enforces_only_from_swarm_configuration() -> None:
    review = enforcement.ENFORCEMENTS["review"]
    assert review.default_mode == "observe"
    assert catalog.defaults()["review"] == "observe"


@pytest.mark.feature("review-queue-gate")
@pytest.mark.parametrize(
    "by,task,state,read_by,accepted",
    [
        (REVIEWER, "r1", "read", (AUTHOR,), True),
        (REVIEWER, "r1", "answered", (AUTHOR,), True),
        (REVIEWER, "r1", "open", (AUTHOR,), False),
        (REVIEWER, "r1", "read", ("another-agent",), False),
        (REVIEWER, "r1", "read", (), False),
        (AUTHOR, "r1", "read", (AUTHOR,), False),
        (REVIEWER, "t1", "read", (AUTHOR,), False),
    ],
)
def test_refusal_requires_independence_and_the_actual_authors_read(
    by: str, task: str, state: str, read_by: tuple[str, ...], accepted: bool
) -> None:
    gate = import_module("scripts.review.gate")
    found = Record(URL, HEAD, (Finding("f1", "Finding", "comment", by, task, state, "answer", read_by),))
    reason = gate.refusal(found, SLUG, URL, HEAD, Who(name=AUTHOR, swarm=SLUG, task="t1"))
    assert (reason is None) is accepted
    if not accepted:
        assert HEAD[:12] in reason
        assert URL in reason


@pytest.mark.feature("review-queue-gate")
def test_refusal_names_missing_authors_reviews_and_each_unread_finding() -> None:
    gate = import_module("scripts.review.gate")
    author = Who(name=AUTHOR, swarm=SLUG, task="t1")
    assert "no author recorded for task t1" in gate.refusal(None, SLUG, URL, HEAD, Who(task="t1"))
    assert "no independent review" in gate.refusal(None, SLUG, URL, HEAD, author)
    assert "no independent review" in gate.refusal(Record(URL, HEAD), SLUG, URL, HEAD, author)
    found = Record(URL, HEAD, tuple(Finding(f"f{i}", "Finding", "comment", REVIEWER, "r1") for i in range(1, 3)))
    reason = gate.refusal(found, SLUG, URL, HEAD, author)
    assert "2 review findings" in reason
    assert "f1, f2" in reason
    assert f"--slug {SLUG}" in reason


@pytest.mark.feature("review-queue-gate")
@pytest.mark.parametrize(
    "mode,kind", [(None, "observe"), ("observe", "observe"), ("invalid", "observe"), ("off", None), ("enforce", "deny")]
)
@pytest.mark.parametrize("reviewed", [False, True])
def test_admission_reads_only_the_observed_head_and_respects_the_swarm_mode(
    monkeypatch: pytest.MonkeyPatch, mode: str | None, kind: str | None, reviewed: bool
) -> None:
    from types import SimpleNamespace

    from scripts.swarm.store import AgentRecord, SwarmError
    from tests.review.fakes import store

    gate = import_module("scripts.review.gate")
    found = store()
    monkeypatch.setattr(found, "config", lambda slug: SimpleNamespace(gates={} if mode is None else {"review": mode}))
    author = Who(name=AUTHOR, swarm=SLUG, task="t1")
    caller = AgentRecord(AUTHOR, "eng", "t1")
    reviewed_record = Record(URL, HEAD, (Finding("f1", "Finding", "comment", REVIEWER, "r1", "read", "", (AUTHOR,)),))
    reads: list[tuple[str, str, str]] = []
    rows: list[log.Row] = []

    def read(metadata: Any, slug: str, url: str, head: str) -> Record | None:
        assert metadata is found
        reads.append((slug, url, head))
        return reviewed_record if reviewed else None

    monkeypatch.setattr(gate.record, "read", read)
    monkeypatch.setattr(log, "append", lambda slug, row: rows.append(row))
    if not reviewed and mode == "enforce":
        with pytest.raises(SwarmError, match="no independent review"):
            gate.admit(found, SLUG, caller, URL, HEAD, author)
    else:
        assert gate.admit(found, SLUG, caller, URL, HEAD, author) is None
    assert reads == [(SLUG, URL, HEAD)]
    assert [(row.gate, row.kind, row.agent, row.task, row.tool) for row in rows] == (
        [] if reviewed or kind is None else [("review", kind, AUTHOR, "t1", "queue")]
    )


@pytest.mark.feature("review-queue-gate")
def test_a_review_of_an_older_head_cannot_admit_a_new_head(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from scripts.review import record
    from scripts.swarm.store import AgentRecord, SwarmError
    from tests.review.fakes import FakeForge, store

    gate = import_module("scripts.review.gate")
    metadata = store()
    monkeypatch.setattr(metadata, "config", lambda slug: SimpleNamespace(gates={"review": "enforce"}))
    monkeypatch.setattr(log, "append", lambda *args: None)
    record.post(metadata, SLUG, FakeForge(), URL, REVIEWER, "r1", ["Review of the old head"])
    author = Who(name=AUTHOR, swarm=SLUG, task="t1")
    with pytest.raises(SwarmError, match="no independent review on head new-head"):
        gate.admit(metadata, SLUG, AgentRecord(AUTHOR, "eng", "t1"), URL, "new-head", author)


@pytest.mark.feature("review-queue-gate")
def test_refusals_quote_twelve_head_characters_and_the_exact_next_step() -> None:
    gate = import_module("scripts.review.gate")
    author = Who(name=AUTHOR, swarm=SLUG, task="t1")
    unread = Record(URL, LONG_HEAD, (Finding("f1", "Finding", "comment", REVIEWER, "r1"),))
    assert gate.refusal(None, SLUG, URL, LONG_HEAD, Who(task="t1")) == (
        f"no author recorded for task t1 on head 0123456789ab of {URL}"
    )
    assert gate.refusal(None, SLUG, URL, LONG_HEAD, author) == (
        f"no independent review on head 0123456789ab of {URL}: a reviewer other than {AUTHOR} "
        "posts its findings with agentihooks review post before this pull request queues"
    )
    assert gate.refusal(unread, SLUG, URL, LONG_HEAD, author) == UNREAD


@pytest.mark.feature("review-queue-gate")
def test_admission_reads_the_callers_swarm_mode_and_logs_the_refusal_it_enforces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from scripts.swarm.store import AgentRecord, SwarmError
    from tests.review.fakes import store

    gate = import_module("scripts.review.gate")
    metadata = store()
    configs: list[str] = []
    logged: list[tuple[str, log.Row]] = []

    def config(slug: str) -> SimpleNamespace:
        configs.append(slug)
        return SimpleNamespace(gates={"review": "enforce"})

    unread = Record(URL, LONG_HEAD, (Finding("f1", "Finding", "comment", REVIEWER, "r1"),))
    monkeypatch.setattr(metadata, "config", config)
    monkeypatch.setattr(gate.record, "read", lambda *args: unread)
    monkeypatch.setattr(log, "append", lambda slug, row: logged.append((slug, row)))
    actor, author = AgentRecord("merge watcher", "system", "t1"), Who(name=AUTHOR, swarm=SLUG, task="t1")
    with pytest.raises(SwarmError) as refused:
        gate.admit(metadata, SLUG, actor, URL, LONG_HEAD, author)
    assert str(refused.value) == UNREAD
    assert configs == [SLUG]
    assert [(slug, row.gate, row.kind, row.agent, row.task, row.tool, row.reason) for slug, row in logged] == [
        (SLUG, "review", "deny", "merge watcher", "t1", "queue", UNREAD)
    ]
