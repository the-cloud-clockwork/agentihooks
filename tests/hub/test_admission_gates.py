"""Hub admission gate verdicts judged against one in memory ledger, and the narrow read contract: an admission never
reads the whole ledger export, however often the revision moves."""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.hub import admission
from scripts.swarm import admission_view, readiness
from scripts.swarm.store import RedisStore, SwarmConfig
from tests.helpers.narrow_ledger import DocLedger
from tests.swarm.test_readiness import doc, task

SLUG = "sw"
EXPORT_MS = 10_000


@pytest.fixture
def gates() -> Iterator[SimpleNamespace]:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "/repo", 2, 2))
    found = doc([task("t1"), task("t2", lane="ci")])
    now = [7_000]

    def slow() -> None:
        now[0] += EXPORT_MS

    ledger: Any = DocLedger(found, moving=True, export=slow)
    rows = {row["id"]: row for row in found["tasks"]}
    admitter = admission.Admission(store, ledger, readiness.Source(store, ledger), clock=lambda: now[0])
    yield SimpleNamespace(store=store, ledger=ledger, rows=rows, found=found, admitter=admitter)
    store.redis.close()


def ask(**fields: Any) -> dict[str, Any]:
    return {"hive": "hive-a", "task": "t1", "lane": "eng", "generation": 0, **fields}


@pytest.mark.feature("hub-admission-narrow-read")
def test_an_admission_never_reads_the_full_export_when_the_revision_moves(
    gates: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    first = gates.admitter.admit(SLUG, ask())
    gates.rows["t2"]["description"] = "edited between admissions"
    second = gates.admitter.admit(SLUG, ask(task="t2", lane="ci"))

    assert first is not None and first["task"]["id"] == "t1"
    assert second is not None and second["task"]["id"] == "t2"
    assert gates.ledger.exports == 0
    assert "late" not in capsys.readouterr().err


@pytest.mark.feature("hub-admission-narrow-read")
@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"freezes": [{"verb": "freeze", "target": "tasks/t1"}]}, "holds it"),
        ({"freezes": [{"verb": "freeze", "target": "phases/p1"}]}, "holds it"),
        ({"freezes": [{"verb": "focus", "target": "tasks/t2"}]}, "a focus elsewhere holds it"),
        ({"phase": "p2"}, "its phase later is waiting on an earlier phase"),
        ({"depends_on": ["t2"]}, "it waits on task t2"),
        ({"enhancement": "needed"}, "it waits for its enhancement plan task"),
    ],
    ids=["frozen", "frozen-phase", "unfocused", "phased", "waits", "thin"],
)
def test_an_admission_refuses_a_task_its_claim_gate_holds_and_claims_nothing(
    gates: SimpleNamespace, change: dict[str, Any], reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    fields = dict(change)
    if "freezes" in fields:
        gates.found["freezes"] = fields.pop("freezes")
    gates.rows["t1"].update(fields)

    assert gates.admitter.admit(SLUG, ask()) is None

    err = capsys.readouterr().err
    assert "task=t1 hive=hive-a refused: the offer is withdrawn" in err
    assert reason in err
    assert gates.store.claimant(SLUG, "t1") is None
    assert gates.ledger.writes == []


@pytest.mark.feature("hub-admission-narrow-read")
def test_an_admission_claims_the_task_its_dependency_left_open_once_the_dependency_is_done(
    gates: SimpleNamespace,
) -> None:
    gates.rows["t1"]["depends_on"] = ["t2"]
    gates.rows["t2"]["state"] = "done"

    admitted = gates.admitter.admit(SLUG, ask())

    assert admitted is not None and admitted["task"]["id"] == "t1"
    assert gates.rows["t1"]["state"] == "claimed"
    assert gates.rows["t1"]["claimed_by"] == admitted["name"]


@pytest.mark.feature("hub-admission-narrow-read")
def test_an_admission_hands_the_continuation_the_rows_on_its_task_branch(
    gates: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import continuation

    asked: list[tuple[str, dict[str, Any]]] = []
    resolved: list[dict[str, Any]] = []
    select = gates.ledger.tasks_where

    def tasks_where(slug: str, **filters: Any) -> list[dict[str, Any]]:
        asked.append((slug, filters))
        return select(slug, **filters)

    def resolve(repo: str, task: dict[str, Any], rows: dict[str, Any], *rest: Any) -> dict[str, Any]:
        resolved.append(rows)
        return {}

    monkeypatch.setattr(gates.ledger, "tasks_where", tasks_where)
    monkeypatch.setattr(continuation, "resolve", resolve)
    gates.rows["t1"]["branch"] = "t1-branch"
    gates.found["tasks"].append(task("t9", state="done", branch="t1-branch", pr_url="https://example.invalid/pull/9"))

    assert gates.admitter.admit(SLUG, ask()) is not None
    assert (SLUG, {"branches": ["t1-branch"], "fields": admission_view.BRANCH_FIELDS}) in asked
    assert "t9" in resolved[0]


@pytest.mark.feature("hub-admission-narrow-read")
def test_an_admission_reads_a_plan_task_steering_through_its_readiness_source(gates: SimpleNamespace) -> None:
    gates.found["tasks"].append(task("plan-p1", kind="plan", lane="plan"))

    gates.admitter.admit(SLUG, ask(task="plan-p1", lane="plan"))

    assert gates.ledger.exports == 1


@pytest.mark.feature("plan-evidence-phase-less-plan-task")
def test_an_admission_claims_a_plan_task_with_no_phase(gates: SimpleNamespace) -> None:
    gates.found["tasks"].append(task("plan-x", phase="", kind="plan", lane="plan"))

    admitted = gates.admitter.admit(SLUG, ask(task="plan-x", lane="plan"))

    assert admitted is not None and admitted["task"]["id"] == "plan-x"
    assert gates.store.claimant(SLUG, "plan-x") == admitted["name"]
