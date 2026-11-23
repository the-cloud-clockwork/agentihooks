from types import SimpleNamespace

import pytest

from scripts.review import gate, record
from scripts.swarm import cli
from scripts.swarm import ledger_client as client
from scripts.swarm.store import RedisStore
from tests.review.fakes import AUTHOR, REVIEWER, SLUG, URL, FakeForge, swarm
from tests.review.test_queue_gate import _queue
from tests.swarm.client_class import client_class

pytestmark = pytest.mark.unit
MASTER = "master@sw"


def _delegated(monkeypatch: pytest.MonkeyPatch, mode: str, owner: str | None) -> RedisStore:
    found = swarm({"review": mode})
    caller = SimpleNamespace(name=MASTER, lane="master", task="master")
    monkeypatch.setattr(cli, "_me", lambda *args: caller)
    monkeypatch.setattr(cli, "_hotfix_master", lambda *args: False)
    monkeypatch.setattr(cli.master_queue, "route", lambda *args: {"route": "focused urgent", "task": "t1"})

    def task(slug: str, task_id: str) -> dict[str, str | None]:
        assert (slug, task_id) == (SLUG, "t1")
        return {"claimed_by": owner}

    monkeypatch.setattr(client, "LedgerClient", client_class(SimpleNamespace(task=task)))
    return found


@pytest.mark.feature("review-findings-queue-gate")
@pytest.mark.parametrize("actor", ["expert", "hotfix", "stalled_master"])
@pytest.mark.parametrize(
    ("mode", "read", "code", "queued", "rows"),
    [
        ("observe", False, 0, [URL], [("review", "observe")]),
        ("enforce", False, 1, [], [("review", "deny")]),
        ("enforce", True, 0, [URL], []),
    ],
)
def test_every_queue_caller_requires_the_independent_review(
    monkeypatch: pytest.MonkeyPatch,
    actor: str,
    mode: str,
    read: bool,
    code: int,
    queued: list[str],
    rows: list[tuple[str, str]],
) -> None:
    if actor == "stalled_master":
        found = _delegated(monkeypatch, mode, AUTHOR)
    else:
        found = swarm({"review": mode})
        caller = SimpleNamespace(name=AUTHOR, lane="expert" if actor == "expert" else "master", task="t1")
        monkeypatch.setattr(cli, "_me", lambda *args: caller)
        monkeypatch.setattr(cli, "_hotfix_master", lambda *args: actor == "hotfix")
    if read:
        forge = FakeForge()
        record.post(found, SLUG, forge, URL, REVIEWER, "r1", ["First"])
        record.mark(found, SLUG, forge, URL, AUTHOR, ["f1"], "")
    actual_code, actual_queued, actual_rows = _queue(monkeypatch, found)
    assert actual_code == code
    assert actual_queued == queued
    assert [(row.gate, row.kind) for row in actual_rows] == rows


@pytest.mark.feature("review-findings-queue-gate")
@pytest.mark.parametrize(
    ("reviewer", "reader", "code"),
    [(REVIEWER, AUTHOR, 0), (REVIEWER, MASTER, 1), (AUTHOR, MASTER, 1)],
)
def test_a_master_queue_requires_the_actual_authors_review_read(
    monkeypatch: pytest.MonkeyPatch, reviewer: str, reader: str, code: int
) -> None:
    found, forge = _delegated(monkeypatch, "enforce", AUTHOR), FakeForge()
    record.post(found, SLUG, forge, URL, reviewer, "r1", ["First"])
    record.mark(found, SLUG, forge, URL, reader, ["f1"], "")
    actual_code, queued, rows = _queue(monkeypatch, found)
    assert actual_code == code
    assert queued == ([URL] if code == 0 else [])
    assert [(row.gate, row.kind) for row in rows] == ([] if code == 0 else [("review", "deny")])


@pytest.mark.feature("review-findings-queue-gate")
@pytest.mark.parametrize(("mode", "code", "kind"), [("observe", 0, "observe"), ("enforce", 1, "deny")])
@pytest.mark.parametrize("owner", ["", None])
def test_a_missing_author_never_falls_back_to_the_queuing_master(
    monkeypatch: pytest.MonkeyPatch, mode: str, code: int, kind: str, owner: str | None
) -> None:
    found, forge = _delegated(monkeypatch, mode, owner), FakeForge()
    record.post(found, SLUG, forge, URL, REVIEWER, "r1", ["First"])
    record.mark(found, SLUG, forge, URL, MASTER, ["f1"], "")
    actual_code, queued, rows = _queue(monkeypatch, found)
    assert actual_code == code
    assert queued == ([URL] if code == 0 else [])
    assert [(row.gate, row.kind) for row in rows] == [("review", kind)]


@pytest.mark.feature("review-findings-queue-gate")
@pytest.mark.parametrize("owner", [AUTHOR, None])
def test_delegated_author_identity_keeps_the_target_swarm_and_task(
    monkeypatch: pytest.MonkeyPatch, owner: str | None
) -> None:
    from scripts.gates.base import Who
    from scripts.swarm.store import AgentRecord

    _delegated(monkeypatch, "enforce", owner)
    caller = AgentRecord(name=MASTER, lane="master", task="master")
    assert gate._author(SLUG, caller, {"task": "t1"}) == Who(name=owner or "", swarm=SLUG, task="t1")
    assert gate._author(SLUG, caller, None) == Who(name=MASTER, swarm=SLUG, task="master")
