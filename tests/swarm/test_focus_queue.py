"""Focus is an exclusive ordered queue: focused work runs first, free work waits for it, frozen work never runs."""

import json
from collections.abc import Sequence
from typing import Any

import pytest

from scripts.swarm import cli, freeze
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm_ledger.repository import hierarchy
from tests.swarm.test_freeze import FrozenLedger, doc, launch, record, spawned, task
from tests.swarm.test_swarm_freeze_cli import acting, swarm  # noqa: F401
from tests.swarm.test_tick import FakeRuntime

pytestmark = [pytest.mark.unit, pytest.mark.feature("swarm-focus")]


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    return s


def gate(row: dict[str, Any], *records: dict[str, Any], tasks: Sequence[dict[str, Any]] = ()) -> bool:
    found = doc(*records, tasks=[row, *tasks])
    return freeze.claim_gate(row, found, hierarchy.project(found)[0], "")


def test_focus_only_a_phase_leaves_every_other_unfrozen_task_free_and_unclaimed_while_a_focused_task_is_open(
    store: RedisStore,
) -> None:
    ledger = FrozenLedger(
        [{"id": "focused", "phase": "p1"}, {"id": "free", "phase": "p2"}, {"id": "later", "phase": "p2"}],
        record("phases/p1", "focus"),
    )
    runtime = FakeRuntime()
    launch(store, ledger, runtime, now_ms=1_000)
    assert spawned(runtime) == ["focused"]
    assert ledger.rows["free"]["state"] == ledger.rows["later"]["state"] == "open"
    ledger.rows["focused"]["state"] = "pr"
    launch(store, ledger, runtime, now_ms=2_000)
    assert spawned(runtime) == ["focused"]
    ledger.rows["focused"]["state"] = "done"
    launch(store, ledger, runtime, now_ms=3_000)
    assert spawned(runtime) == ["focused", "free", "later"]


@pytest.mark.parametrize("state", ["open", "claimed", "blocked", "pr"])
def test_a_focused_task_that_is_not_done_holds_every_task_outside_the_focus(state: str) -> None:
    focused = task("hot", phase="p2", state=state)
    assert gate(task(), record("tasks/hot", "focus"), tasks=[focused])
    assert not gate(task(), record("tasks/hot", "focus"), tasks=[{**focused, "state": "done"}])
    assert not gate(task(), record("tasks/hot", "focus"), tasks=[{**focused, "out_of_scope": True}])


def test_a_focus_covering_no_task_holds_nothing() -> None:
    assert not gate(task(), record("plans/b", "focus"))
    assert freeze.admitted(doc(record("plans/b", "focus")), {}) is None


def test_the_tasks_a_focused_task_waits_on_join_the_focus() -> None:
    focused = task("hot", phase="p2", depends_on=["dep"], parked_on=["parked"])
    dep, parked = task("dep", depends_on=["deeper"]), task("parked")
    deeper, other = task("deeper"), task("other")
    rows = [focused, dep, parked, deeper, other]
    found = doc(record("tasks/hot", "focus"), tasks=rows)
    graph = hierarchy.project(found)[0]
    assert freeze.admitted(found, graph) == {"hot", "dep", "parked", "deeper"}
    assert [row["id"] for row in rows if freeze.claim_gate(row, found, graph, "")] == ["other"]


def test_tasks_that_wait_on_each_other_join_the_focus_once() -> None:
    rows = [task("hot", phase="p2", depends_on=["loop"]), task("loop", depends_on=["hot", "tail"]), task("tail")]
    found = doc(record("tasks/hot", "focus"), tasks=[*rows, task("other")])
    assert freeze.admitted(found, hierarchy.project(found)[0]) == {"hot", "loop", "tail"}


def test_a_frozen_task_inside_the_focus_never_holds_the_queue_open() -> None:
    inside = task("inside", phase="p2")
    assert not gate(task(), record("plans/b", "focus"), record("tasks/inside"), tasks=[inside])
    assert gate(inside, record("plans/b", "focus"), record("tasks/inside"))


def test_the_admitted_set_is_computed_once_per_ledger_view(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = freeze.ancestry
    monkeypatch.setattr(freeze, "ancestry", lambda row, graph: calls.append(row["id"]) or real(row, graph))
    graph: dict[str, Any] = {}
    found = doc(record("tasks/hot", "focus"), tasks=[task("hot", phase="p2")])
    assert freeze.admitted(found, graph) == freeze.admitted(found, graph) == {"hot"}
    assert calls == ["hot"]
    found["tasks"][0]["state"] = "done"
    assert freeze.admitted(found, graph) is None
    found["freezes"] = [record("tasks/other", "focus")]
    found["tasks"] = [task("other")]
    assert freeze.admitted(found, graph) == {"other"}
    assert freeze.admitted({**found}, graph) == {"other"}
    assert freeze.admitted(found, {}) == {"other"}
    assert calls == ["hot", "other", "other", "other"]


def test_a_new_focus_list_on_the_same_ledger_view_recomputes_the_admitted_set() -> None:
    graph: dict[str, Any] = {}
    found = doc(record("tasks/a", "focus"), tasks=[task("a"), task("b")])
    assert freeze.admitted(found, graph) == {"a"}
    found["freezes"] = [record("tasks/b", "focus")]
    assert freeze.admitted(found, graph) == {"b"}


def test_the_tasks_of_a_phase_the_focused_phase_waits_on_join_the_focus() -> None:
    phases = [{"id": "p1"}, {"id": "p2", "depends_on": ["p1", "empty"]}, {"id": "p3", "depends_on": ["p2"]}]
    rows = [task("early", phase="p1"), task("middle", phase="p2"), task("late", phase="p3"), task("other", phase="p4")]
    found = {**doc(record("tasks/late", "focus"), tasks=rows), "phases": phases}
    assert freeze.admitted(found, hierarchy.project(found)[0]) == {"late", "middle", "early"}


def test_a_focused_task_waiting_on_frozen_work_never_holds_the_queue_open() -> None:
    focused = task("hot", phase="p2", depends_on=["middle"])
    middle, cold = task("middle", depends_on=["cold"]), task("cold")
    rows = [focused, middle, cold]
    assert freeze.admitted(doc(record("tasks/hot", "focus"), record("tasks/cold"), tasks=rows), {}) is None
    assert not gate(task("free"), record("tasks/hot", "focus"), record("tasks/cold"), tasks=rows)
    assert freeze.admitted(doc(record("tasks/hot", "focus"), tasks=rows), {}) == {"hot", "middle", "cold"}
    done = [focused, middle, {**cold, "state": "done"}]
    assert freeze.admitted(doc(record("tasks/hot", "focus"), record("tasks/cold"), tasks=done), {}) == {
        "hot",
        "middle",
        "cold",
    }


def test_done_work_between_a_focused_task_and_frozen_work_keeps_the_focus_open() -> None:
    rows = [task("hot", phase="p2", depends_on=["middle"]), task("middle", state="done", depends_on=["cold"])]
    found = doc(record("tasks/hot", "focus"), record("tasks/cold"), tasks=[*rows, task("cold")])
    assert freeze.admitted(found, {}) == {"hot", "middle"}


def test_focus_weight_never_lets_free_work_take_a_seat_from_focused_work(store: RedisStore) -> None:
    store.update("sw", max_eng=3, focus_weight=50, autonomy="delegate")
    ledger = FrozenLedger(
        [{"id": "f1", "phase": "p1"}, {"id": "f2", "phase": "p1"}, {"id": "free", "phase": "p2", "rank": "urgent"}],
        record("phases/p1", "focus"),
    )
    runtime = FakeRuntime()
    launch(store, ledger, runtime, now_ms=1_000)
    assert sorted(spawned(runtime)) == ["f1", "f2"]
    assert ledger.rows["free"]["state"] == "open"


def test_the_focus_queue_order_sets_the_claim_order(store: RedisStore) -> None:
    store.update("sw", max_eng=1)
    rows = [{"id": "bottom", "phase": "p1"}, {"id": "top", "phase": "p2"}]
    runtime = FakeRuntime()
    launch(
        store,
        FrozenLedger(rows, record("tasks/top", "focus"), record("tasks/bottom", "focus")),
        runtime,
        now_ms=1_000,
    )
    assert spawned(runtime) == ["top"]


def test_the_reversed_focus_queue_claims_the_other_task_first(store: RedisStore) -> None:
    store.update("sw", max_eng=1)
    rows = [{"id": "top", "phase": "p2"}, {"id": "bottom", "phase": "p1"}]
    runtime = FakeRuntime()
    launch(
        store,
        FrozenLedger(rows, record("tasks/bottom", "focus"), record("tasks/top", "focus")),
        runtime,
        now_ms=1_000,
    )
    assert spawned(runtime) == ["bottom"]


def test_a_focus_beneath_a_freeze_unfreezes_the_task_and_a_freeze_beneath_a_focus_holds_it() -> None:
    assert not gate(task(), record("phases/p1"), record("tasks/t1", "focus"))
    assert gate(task(), record("tasks/t1"), record("phases/p1", "focus"))
    assert not gate(task(kind="research"), record("lane:eng"), record("kind:research", "focus"))


def test_an_unfrozen_task_is_free_and_claimed_only_after_the_focus_queue_is_empty(store: RedisStore) -> None:
    ledger = FrozenLedger(
        [{"id": "focused", "phase": "p1"}, {"id": "thawed", "phase": "p2"}],
        record("tasks/focused", "focus"),
        record("tasks/thawed"),
    )
    runtime = FakeRuntime()
    launch(store, ledger, runtime, now_ms=1_000)
    assert spawned(runtime) == ["focused"]
    ledger.freezes = [record("tasks/focused", "focus")]
    launch(store, ledger, runtime, now_ms=2_000)
    assert spawned(runtime) == ["focused"]
    ledger.rows["focused"]["state"] = "done"
    launch(store, ledger, runtime, now_ms=3_000)
    assert spawned(runtime) == ["focused", "thawed"]


def test_urgent_rank_never_bypasses_the_focus_queue(store: RedisStore) -> None:
    ledger = FrozenLedger(
        [{"id": "urgent", "phase": "p2", "rank": "urgent"}, {"id": "focused", "phase": "p1", "rank": "low"}],
        record("plans/a", "focus"),
    )
    runtime = FakeRuntime()
    launch(store, ledger, runtime, now_ms=1_000)
    assert spawned(runtime) == ["focused"]
    assert ledger.rows["urgent"]["state"] == "open"
    assert gate(task(rank="urgent"), record("tasks/hot", "focus"), tasks=[task("hot", phase="p2")])


def test_an_urgent_task_held_by_the_focus_names_the_focus() -> None:
    from scripts.swarm import claim_hold

    row, hot = task(rank="urgent"), task("hot", phase="p2")
    found = doc(record("tasks/hot", "focus"), tasks=[row, hot])
    rows = {t["id"]: t for t in found["tasks"]}
    assert claim_hold.urgent(row, rows, found, hierarchy.project(found)[0], "") == claim_hold.FOCUS
    assert claim_hold.urgent(row, rows, found, hierarchy.project(found)[0], "p1") == ""


def test_the_focus_command_passes_only_and_the_queue_position(
    swarm: tuple[RedisStore, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, ledger = swarm
    acting(monkeypatch)
    assert cli.main(["demo", "focus", "phases/p1", "--only"]) == 0
    assert cli.main(["demo", "focus", "tasks/t1", "--top"]) == 0
    assert cli.main(["demo", "focus", "tasks/t2", "--bottom"]) == 0
    assert [entry[3] for entry in ledger.freezes] == [
        {"by": None, "reason": "", "quote": "", "only": True},
        {"by": None, "reason": "", "quote": "", "position": "top"},
        {"by": None, "reason": "", "quote": "", "position": "bottom"},
    ]
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()][0] == {
        "focus": "phases/p1",
        "by": "operator",
        "only": True,
    }


def test_top_and_bottom_together_are_refused(swarm: tuple[RedisStore, Any], monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    acting(monkeypatch)
    with pytest.raises(SystemExit):
        cli.main(["demo", "focus", "tasks/t1", "--top", "--bottom"])


def test_the_client_sends_the_queue_fields_only_on_a_focus(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm.ledger_client import FreezeWords, LedgerClient

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(LedgerClient, "_call", lambda self, slug, ops: sent.append(ops[0]))
    LedgerClient().freeze("demo", "focus", "plans/a", words=FreezeWords(position="top", only=True))
    LedgerClient().freeze(
        "demo", "unfreeze", "plans/a", words=FreezeWords(position="top", only=True, quote="his words")
    )
    assert {key: sent[0][key] for key in ("op", "verb", "target", "position", "only")} == {
        "op": "freeze_set",
        "verb": "focus",
        "target": "plans/a",
        "position": "top",
        "only": True,
    }
    assert "position" not in sent[1] and "only" not in sent[1]
    assert sent[1]["quote"] == "his words"
