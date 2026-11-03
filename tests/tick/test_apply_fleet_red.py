"""The fleet red notice step returns guarded hold writes that its applier posts with compare and set."""

from dataclasses import dataclass, field
from typing import Any

import pytest

import scripts.swarm.cli  # noqa: F401
from scripts.swarm import fleet_red
from scripts.swarm.ledger_client import LedgerRefused
from scripts.tick import engine
from scripts.tick.deadline import Deadline
from scripts.tick.ports.records import RevisionConflict
from scripts.tick.step import Context, Domain, Write
from scripts.tick.steps import PASS, actions, tasks

pytestmark = pytest.mark.unit

URL = "https://github.com/o/r/pull/1"
EXPECTED = {"state": "open", "pr_url": URL, "claimed_by": ""}
HOLD = Write(tasks.FLEET_RED, "t1", {"url": URL, "checks": "Tests"}, expected=EXPECTED)
SAVE = Write(fleet_red.LISTINGS, "sw:fleet-red-pulls:o/r", "[]")


@dataclass
class Redis:
    saved: dict[str, str] = field(default_factory=dict)

    def set(self, name: str, value: str, nx: bool = False) -> bool:
        assert nx
        if name in self.saved:
            return False
        self.saved[name] = value
        return True

    def delete(self, *names: str) -> None:
        for name in names:
            self.saved.pop(name)


@dataclass
class Store:
    claims: set[str] = field(default_factory=set)
    redis: Redis = field(default_factory=Redis)

    def key(self, slug: str, part: str) -> str:
        return f"{slug}:{part}"

    def claimed(self, slug: str, task_ids: list[str]) -> set[str]:
        assert slug == "sw"
        return self.claims & set(task_ids)


@dataclass
class Ledger:
    refuse: bool = False
    notices: list[tuple[str, str, str]] = field(default_factory=list)

    def notice(self, slug: str, task_id: str, text: str) -> None:
        if self.refuse:
            raise LedgerRefused("refused")
        self.notices.append((slug, task_id, text))


def _task(**fields: str) -> dict[str, Any]:
    return {"id": "t1", "state": "open", "pr_url": URL, "claimed_by": "", **fields}


def test_the_fleet_red_step_returns_guarded_hold_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Any, ...]] = []

    def holds(
        slug: str, store: Any, rows: Any, now_ms: int
    ) -> tuple[list[tuple[str, str, str]], list[str], list[Write]]:
        seen.append((slug, store, rows, now_ms))
        return [("t1", URL, "Tests"), ("t2", URL, "Lint, Tests")], ["sw:fleet-red:t3", "sw:fleet-red:t4"], [SAVE]

    monkeypatch.setattr(fleet_red, "holds", holds)
    found = tasks.fleet_red_writes("sw", "store", {"t1": {}}, 7)
    assert seen == [("sw", "store", {"t1": {}}, 7)]
    assert found == [
        SAVE,
        HOLD,
        f"task t1 held unclaimed, {URL} is red only from Tests",
        Write(tasks.FLEET_RED, "t2", {"url": URL, "checks": "Lint, Tests"}, expected={**EXPECTED}),
        f"task t2 held unclaimed, {URL} is red only from Lint, Tests",
        Write(tasks.FLEET_RED_LIFTED, "sw", "sw:fleet-red:t3\nsw:fleet-red:t4"),
    ]


def test_the_fleet_red_step_returns_no_lifted_write_when_no_hold_lifted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet_red, "holds", lambda *args: ([], [], []))
    assert tasks.fleet_red_writes("sw", "store", {}, 7) == []


def test_the_fleet_red_step_returns_listing_saves_without_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet_red, "holds", lambda *args: ([], [], [SAVE]))
    assert tasks.fleet_red_writes("sw", "store", {}, 7) == [SAVE]


def test_the_lifted_applier_forgets_every_hold_mark_it_names() -> None:
    store = Store()
    store.redis.saved = {"sw:fleet-red:t1": URL, "sw:fleet-red:t2": URL, "sw:fleet-red:t3": URL}
    tasks.lifted_writer(store)(Write(tasks.FLEET_RED_LIFTED, "sw", "sw:fleet-red:t1\nsw:fleet-red:t3"))
    assert store.redis.saved == {"sw:fleet-red:t2": URL}


def test_the_fleet_red_applier_posts_while_the_task_stays_open_and_unclaimed() -> None:
    store, ledger = Store(), Ledger()
    tasks.fleet_red_writer("sw", store, ledger, {"t1": _task()}.get)(HOLD)
    assert ledger.notices == [("sw", "t1", fleet_red.NOTICE.format(url=URL, checks="Tests"))]
    assert store.redis.saved == {"sw:fleet-red:t1": URL}


@pytest.mark.parametrize(
    ("moved", "names"),
    [({"claimed_by": "engineer@a"}, "claimed_by"), ({"state": "pr", "pr_url": ""}, "pr_url, state")],
)
def test_the_fleet_red_applier_drops_a_hold_whose_task_moved(moved: dict[str, str], names: str) -> None:
    store, ledger = Store(), Ledger()
    with pytest.raises(RevisionConflict, match=f"^task t1 changed {names} since the step read it$"):
        tasks.fleet_red_writer("sw", store, ledger, {"t1": _task(**moved)}.get)(HOLD)
    assert (ledger.notices, store.redis.saved) == ([], {})


def test_the_fleet_red_applier_reads_a_missing_task_field_as_empty() -> None:
    store, ledger = Store(), Ledger()
    task = {"id": "t1", "state": "open", "pr_url": URL}
    tasks.fleet_red_writer("sw", store, ledger, {"t1": task}.get)(HOLD)
    assert ledger.notices == [("sw", "t1", fleet_red.NOTICE.format(url=URL, checks="Tests"))]


def test_the_fleet_red_applier_drops_a_hold_a_seat_claimed() -> None:
    store, ledger = Store(claims={"t1"}), Ledger()
    with pytest.raises(RevisionConflict, match="^task t1 was claimed since the step read it$"):
        tasks.fleet_red_writer("sw", store, ledger, {}.get)(HOLD)
    assert (ledger.notices, store.redis.saved) == ([], {})


def test_the_fleet_red_applier_forgets_a_refused_notice_so_the_next_pass_retries_it() -> None:
    store = Store()
    with pytest.raises(RevisionConflict, match="^the hold notice of task t1 failed: LedgerRefused$"):
        tasks.fleet_red_writer("sw", store, Ledger(refuse=True), {}.get)(HOLD)
    assert store.redis.saved == {}


def test_the_fleet_red_applier_drops_any_failed_notice_as_a_conflict_and_forgets_it() -> None:
    class Broken(Ledger):
        def notice(self, slug: str, task_id: str, text: str) -> None:
            raise OSError("down")

    store = Store()
    with pytest.raises(RevisionConflict, match="^the hold notice of task t1 failed: OSError$"):
        tasks.fleet_red_writer("sw", store, Broken(), {}.get)(HOLD)
    assert store.redis.saved == {}


@pytest.mark.parametrize("value", [{"url": URL}, {"url": URL, "checks": 3}, "Tests"])
def test_the_fleet_red_applier_drops_a_write_that_names_no_pull_request_and_checks(value: object) -> None:
    store, ledger = Store(), Ledger()
    with pytest.raises(RevisionConflict, match="^the hold of task t1 names no pull request and checks$"):
        tasks.fleet_red_writer("sw", store, ledger, {}.get)(Write(tasks.FLEET_RED, "t1", value, expected=EXPECTED))
    assert (ledger.notices, store.redis.saved) == ([], {})


def test_appliers_add_fleet_red_only_with_a_store() -> None:
    assert sorted(tasks.appliers({"slug": "sw", "ledger": Ledger()})) == [tasks.GROUPS, tasks.SIZES]
    both = tasks.appliers({"slug": "sw", "ledger": Ledger(), "store": Store()})
    assert sorted(both) == [
        tasks.FLEET_RED,
        tasks.FLEET_RED_LIFTED,
        fleet_red.LISTINGS,
        tasks.GROUPS,
        tasks.RANK,
        tasks.SIZES,
    ]


def test_the_fleet_red_step_declares_the_listings_it_returns() -> None:
    step = next(step for step in tasks.STEPS if step.name == "fleet red notice")
    assert step.writes == {tasks.FLEET_RED, tasks.FLEET_RED_LIFTED, fleet_red.LISTINGS, "actions:fleet red notice"}


class _State:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state

    def read(self, domain: Domain, key: str) -> object | None:
        return self.state.get(key) if domain == PASS else None


def test_a_hold_a_seat_claims_mid_pass_is_dropped_and_recomputed_on_the_next_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, ledger = Store(), Ledger()
    rival = ["engineer@a"]

    def holds(
        slug: str, held_store: Any, rows: Any, now_ms: int
    ) -> tuple[list[tuple[str, str, str]], list[str], list[Write]]:
        if rival:
            store.claims.add("t1")
            rival.clear()
        return [("t1", URL, "Tests")], [], []

    monkeypatch.setattr(fleet_red, "holds", holds)
    step = next(step for step in tasks.STEPS if step.name == "fleet red notice")
    state = {"slug": "sw", "store": store, "rows": {}, "now_ms": 7, "box": {}}

    def one_pass() -> tuple[engine.StepRecord, ...]:
        ctx = Context(_State(state), Deadline.after(60.0))
        return engine.run_group(tasks.REOPENS, ctx, [step], apply=tasks.appliers({**state, "ledger": ledger}))

    class Tick:
        @staticmethod
        def skip_refused(function: Any, *args: Any) -> Any:
            return function(*args)

    monkeypatch.setattr(tasks.steps, "import_module", lambda name: Tick)
    (first,) = one_pass()
    assert first.dropped == (HOLD,)
    assert ledger.notices == []
    assert actions([first])[-1] == (
        "dropped write fleet red t1 of step fleet red notice: task t1 was claimed since the step read it; "
        "the next pass recomputes it"
    )
    store.claims.clear()
    (second,) = one_pass()
    assert second.dropped == ()
    assert ledger.notices == [("sw", "t1", fleet_red.NOTICE.format(url=URL, checks="Tests"))]
