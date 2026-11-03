"""The rank pass step returns its decided pass as one write that its applier takes while the saved heads hold."""

from dataclasses import dataclass, field
from functools import partial
from typing import Any

import pytest

import scripts.swarm.cli  # noqa: F401
from scripts.swarm import dispatcher
from scripts.tick import engine
from scripts.tick.deadline import Deadline
from scripts.tick.ports.records import RevisionConflict
from scripts.tick.step import Context, Domain, Result, Write
from scripts.tick.steps import PASS, actions, place, tasks

pytestmark = pytest.mark.unit

HEAD = "sw:dispatch-head"


@dataclass
class Redis:
    saved: dict[str, str] = field(default_factory=dict)

    def get(self, name: str) -> str | None:
        return self.saved.get(name)


@dataclass
class Store:
    redis: Redis = field(default_factory=Redis)

    def key(self, slug: str, part: str) -> str:
        return f"{slug}:{part}"


def ranking(head: str | None, at_ms: int = 7) -> dispatcher.Ranking:
    return dispatcher.Ranking(at_ms, head, [], {"eng": []})


def test_the_rank_step_returns_its_decided_pass_guarded_on_the_heads_it_read() -> None:
    calls: list[tuple[Any, ...]] = []
    decided = ranking('{"eng": ["a"]}')

    def rank_decision(*args: Any) -> dispatcher.Ranking:
        calls.append(args)
        return decided

    tick = type("Tick", (), {"dispatcher": type("D", (), {"rank_decision": staticmethod(rank_decision)})})
    found = tasks.rank_writes(tick, "sw", "config", "store", "ledger", {"tasks": []}, 7)
    assert found == [Write(tasks.RANK, "sw", decided, expected='{"eng": ["a"]}')]
    assert calls == [("sw", "config", "store", "ledger", {"tasks": []}, 7)]


def test_the_rank_applier_takes_the_pass_while_the_saved_heads_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    taken: list[tuple[Any, ...]] = []

    def apply_ranking(slug: str, store: Any, decided: dispatcher.Ranking) -> list[str]:
        taken.append((slug, store, decided))
        return ["put task a at the head of the eng claim queue"]

    monkeypatch.setattr(dispatcher, "apply_ranking", apply_ranking)
    store = Store(Redis({HEAD: '{"eng": []}'}))
    decided = ranking('{"eng": []}')
    found = tasks.rank_writer(store)(Write(tasks.RANK, "sw", decided, expected='{"eng": []}'))
    assert found == ["put task a at the head of the eng claim queue"]
    assert taken == [("sw", store, decided)]


def test_the_rank_applier_takes_a_first_pass_with_no_saved_heads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher, "apply_ranking", lambda slug, store, decided: [slug])
    assert tasks.rank_writer(Store())(Write(tasks.RANK, "sw", ranking(None), expected=None)) == ["sw"]


def test_the_rank_applier_drops_a_pass_whose_heads_moved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher, "apply_ranking", lambda *args: pytest.fail("took a dropped pass"))
    store = Store(Redis({HEAD: '{"eng": ["b"]}'}))
    with pytest.raises(RevisionConflict, match="^the claim queue heads of swarm sw changed since the rank pass read"):
        tasks.rank_writer(store)(Write(tasks.RANK, "sw", ranking('{"eng": []}'), expected='{"eng": []}'))


def test_the_rank_applier_takes_a_proposing_pass_whatever_the_saved_heads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher, "apply_ranking", lambda slug, store, decided: ["proposed"])
    store = Store(Redis({HEAD: '{"eng": ["b"]}'}))
    proposing = dispatcher.Ranking(7, None, [], None)
    assert tasks.rank_writer(store)(Write(tasks.RANK, "sw", proposing, expected=None)) == ["proposed"]


def test_the_rank_applier_drops_a_write_that_carries_no_rank_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher, "apply_ranking", lambda *args: pytest.fail("took a dropped pass"))
    store = Store(Redis({HEAD: '{"eng": []}'}))
    with pytest.raises(RevisionConflict, match="^the rank write of swarm sw carries no rank pass$"):
        tasks.rank_writer(store)(Write(tasks.RANK, "sw", {"head": '{"eng": []}'}, expected='{"eng": []}'))


def test_appliers_add_rank_only_with_a_store() -> None:
    assert tasks.RANK not in tasks.appliers({"slug": "sw", "ledger": object()})
    store = Store()
    found = tasks.appliers({"slug": "sw", "ledger": object(), "store": store})[tasks.RANK]
    assert isinstance(found, partial)
    assert (found.func, found.args) == (tasks._apply_ranking, (store,))


def test_the_engine_keeps_the_actions_an_applier_returns_after_the_step_actions() -> None:
    first, second = Write(Domain("a"), "k", 1), Write(Domain("b"), "k", 2)
    record = engine.StepRecord("rank pass", 0.0, 1.0, result=Result((first, second)))
    found = engine._applied(record, {Domain("a"): lambda write: ["one", "two"], Domain("b"): lambda write: None})
    assert found.applied == ("one", "two")
    assert actions([found]) == ["one", "two"]
    assert engine._applied(record, {Domain("b"): lambda write: None}) is record


def test_the_engine_keeps_applied_actions_beside_a_later_dropped_write() -> None:
    def conflict(write: Write) -> list[str] | None:
        raise RevisionConflict("moved")

    first, second = Write(Domain("a"), "k", 1), Write(Domain("b"), "k", 2)
    record = engine.StepRecord("rank pass", 0.0, 1.0, result=Result((first, second)))
    found = engine._applied(record, {Domain("a"): lambda write: ["one"], Domain("b"): conflict})
    assert (found.applied, found.dropped, found.conflicts) == (("one",), (second,), ("moved",))
    assert actions([found]) == ["one", "dropped write b k of step rank pass: moved; the next pass recomputes it"]


class _State:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state

    def read(self, domain: Domain, key: str) -> object | None:
        return self.state.get(key) if domain == PASS else None


def test_a_rank_pass_whose_heads_another_pass_moved_is_dropped_and_recomputed_on_the_next_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(Redis({HEAD: '{"eng": []}'}))
    rival = ['{"eng": ["b"]}']
    taken: list[str | bytes | None] = []

    def rank_decision(*args: Any) -> dispatcher.Ranking:
        read = store.redis.get(HEAD)
        if rival:
            store.redis.saved[HEAD] = rival.pop()
        return ranking(read)

    def apply_ranking(slug: str, held_store: Any, decided: dispatcher.Ranking) -> list[str]:
        taken.append(decided.head)
        return ["put task a at the head of the eng claim queue"]

    class Tick:
        dispatcher = type("D", (), {"rank_decision": staticmethod(rank_decision)})

        @staticmethod
        def skip_refused(function: Any, *args: Any) -> Any:
            return function(*args)

    monkeypatch.setattr(dispatcher, "apply_ranking", apply_ranking)
    monkeypatch.setattr(tasks.steps, "import_module", lambda name: Tick)
    step = next(step for step in place.STEPS if step.name == "rank pass")
    state = {"slug": "sw", "config": "c", "store": store, "ledger": "l", "doc": {}, "now_ms": 7, "box": {}}

    def one_pass() -> tuple[engine.StepRecord, ...]:
        ctx = Context(_State(state), Deadline.after(60.0))
        return engine.run_group(place.PLACEMENT, ctx, [step], apply=tasks.appliers(state))

    (first,) = one_pass()
    assert [write.expected for write in first.dropped] == ['{"eng": []}']
    assert taken == []
    assert actions([first]) == [
        "dropped write rank sw of step rank pass: the claim queue heads of swarm sw changed since the rank pass "
        "read them; the next pass recomputes it"
    ]
    (second,) = one_pass()
    assert second.dropped == ()
    assert taken == ['{"eng": ["b"]}']
    assert actions([second]) == ["put task a at the head of the eng claim queue"]
