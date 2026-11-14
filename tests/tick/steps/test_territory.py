"""Territory overlap, parked and stacked dependencies and the settle and close of a swarm live in the territory step
module and the swarm tick module re-exports each name."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import stack, tick
from scripts.swarm.store import AgentRecord
from scripts.tick.steps import territory

pytestmark = pytest.mark.unit

MOVED = (
    "_parked",
    "_stack_base",
    "_stacked_dep",
    "_refund_parked",
    "_overlaps",
    "_shared",
    "_sharing",
    "_area",
    "_nested",
    "_settle",
    "_end_awaiting_seats",
    "_close_space",
    "_bin_closed",
)


@pytest.mark.feature("swarm-tick-territory")
@pytest.mark.parametrize("name", MOVED)
def test_tick_reexports_the_moved_name(name: str) -> None:
    moved = getattr(territory, name)
    assert moved.__module__ == territory.__name__
    assert getattr(tick, name) is moved


@pytest.mark.feature("swarm-tick-territory")
def test_the_tick_module_holds_no_moved_definition() -> None:
    text = Path(str(tick.__file__)).read_text(encoding="utf-8")
    assert [name for name in MOVED if f"\ndef {name}(" in text] == []


@pytest.mark.feature("swarm-tick-territory")
def test_moved_territory_code_reaches_tick_seams_through_the_tick_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tick, "_shared", lambda mine, theirs: ["scripts"] if mine == ["a"] else [])
    assert territory._overlaps(["a"], ["b"]) is True
    assert territory._overlaps(["c"], ["b"]) is False


@pytest.mark.feature("swarm-tick-territory")
@pytest.mark.parametrize(
    ("mine", "theirs", "shared"),
    [
        (["scripts/swarm/"], ["scripts/swarm/tick.py"], ["scripts/swarm/tick.py"]),
        (["./scripts/swarm/tick.py"], ["scripts/swarm"], ["scripts/swarm/tick.py"]),
        (["scripts/swarm"], ["scripts/swarmy"], []),
        (["docs"], ["docs"], ["docs"]),
    ],
)
def test_shared_names_the_narrower_of_two_nested_areas(mine: list[str], theirs: list[str], shared: list[str]) -> None:
    assert territory._shared(mine, theirs) == shared
    assert territory._overlaps(mine, theirs) is bool(shared)


@pytest.mark.feature("swarm-tick-territory")
def test_a_parked_task_waits_until_every_parked_dependency_is_done() -> None:
    rows = {"a": {"state": "done"}, "b": {"state": "claimed"}}
    assert territory._parked({"parked_on": ["a", "b"]}, rows) is True
    assert territory._parked({"parked_on": ["a"]}, rows) is False
    assert territory._parked({}, rows) is False
    assert territory._parked({"parked_on": ["a", "gone"]}, rows) is True


@pytest.mark.feature("swarm-tick-territory")
def test_a_stacked_dependency_names_its_repo_only_when_it_lives_elsewhere() -> None:
    row = {"branch": "b1", "branch_repo": "https://example.test/other.git"}
    key = stack.repo_key(row["branch_repo"])
    assert territory._stacked_dep("d1", {"branch": "b1"}, "repo", lambda repo: "x") == {"task": "d1", "branch": "b1"}
    assert territory._stacked_dep("d1", row, "repo", lambda repo: key if repo == "repo" else "") == {
        "task": "d1",
        "branch": "b1",
    }
    elsewhere = {"task": "d1", "branch": "b1", "repo": "https://example.test/other.git"}
    assert territory._stacked_dep("d1", row, "repo", lambda repo: "local") == elsewhere


@pytest.mark.feature("swarm-tick-territory")
def test_close_space_closes_only_an_empty_swarm() -> None:
    closed: list[object] = []
    runtime = SimpleNamespace(close_space=closed.append)
    territory._close_space("sw", "config", SimpleNamespace(agents=lambda slug: ["one"]), runtime)
    assert closed == []
    assert territory._close_space("sw", "config", SimpleNamespace(agents=lambda slug: []), runtime) == []
    assert closed == ["config"]


class _RefundStore:
    """A store answering one handoff envelope and recording each refunded claim."""

    def __init__(self, envelope: dict[str, str] | None) -> None:
        self.envelope = envelope
        self.asked: list[tuple[str, str]] = []
        self.refunded: list[tuple[str, str, str, int]] = []

    def handoff_envelope(self, slug: str, task_id: str) -> dict[str, str] | None:
        self.asked.append((slug, task_id))
        return self.envelope

    def claim_generation(self, slug: str, task_id: str) -> int:
        return 7 if (slug, task_id) == ("sw", "t1") else 0

    def refund_held_claim(self, slug: str, task_id: str, name: str, generation: int) -> None:
        self.refunded.append((slug, task_id, name, generation))


@pytest.mark.feature("swarm-tick-territory")
@pytest.mark.parametrize(
    ("envelope", "victim", "parked_on", "refunded"),
    [
        (None, False, [], False),
        ({"reason": "quota"}, False, [], True),
        ({"reason": "operator"}, True, [], True),
        ({"reason": "operator"}, False, [], False),
        ({"reason": "context"}, True, [], False),
        ({}, False, ["t0"], True),
    ],
)
def test_refund_parked_returns_the_claim_of_a_parked_quota_or_preempted_handoff(
    monkeypatch: pytest.MonkeyPatch,
    envelope: dict[str, str] | None,
    victim: bool,
    parked_on: list[str],
    refunded: bool,
) -> None:
    taken: list[tuple[object, str, str]] = []

    def take_victim(store: object, slug: str, name: str) -> bool:
        taken.append((store, slug, name))
        return victim

    monkeypatch.setattr(tick.preempt, "take_victim", take_victim)
    store = _RefundStore(envelope)
    rows = {"t0": {"state": "claimed"}, "t1": {"parked_on": parked_on}}
    agent = AgentRecord("engineer@1-1", "eng", "t1")
    territory._refund_parked("sw", store, rows, agent)
    assert store.asked == [("sw", "t1")]
    assert taken == [(store, "sw", "engineer@1-1")]
    assert store.refunded == ([("sw", "t1", "engineer@1-1", 7)] if refunded else [])


@pytest.mark.feature("swarm-tick-territory")
def test_refund_parked_reads_a_task_missing_from_the_rows_as_not_parked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tick.preempt, "take_victim", lambda store, slug, name: False)
    store = _RefundStore({"reason": "context"})
    territory._refund_parked("sw", store, {"t0": {"state": "claimed"}}, AgentRecord("engineer@1-1", "eng", "t1"))
    assert store.refunded == []


class _SettleStore:
    """A store holding the swarm's agents and recording each state update."""

    def __init__(self, agents: list[AgentRecord]) -> None:
        self.held = agents
        self.updates: list[tuple[str, dict[str, str]]] = []

    def agents(self, slug: str) -> list[AgentRecord]:
        return self.held if slug == "sw" else []

    def update(self, slug: str, **changes: str) -> None:
        self.updates.append((slug, changes))


class _Ledger:
    """A ledger recording each notice."""

    def __init__(self) -> None:
        self.notices: list[tuple[str, str]] = []

    def notify(self, slug: str, text: str) -> None:
        self.notices.append((slug, text))


def _agent(name: str, lane: str, state: str = "working") -> AgentRecord:
    return AgentRecord(name, lane, "t1", state=state)


def _settled(config: Any, store: Any, ledger: Any, rows: dict[str, dict[str, Any]], doc: dict[str, Any]) -> list[str]:
    return territory._settle("sw", config, store, ledger, rows, doc)


def _settle_seams(monkeypatch: pytest.MonkeyPatch, claimable: bool = False) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def claimable_in(slug: str, store: object, rows: object, doc: object, lane: str) -> list[str]:
        calls.append(("claimable", lane))
        return ["t9"] if claimable and lane == "ci" else []

    def ended(slug: str, store: object, ledger: object, rows: object, agents: list[AgentRecord]) -> None:
        calls.append(("end", [a.name for a in agents]))

    monkeypatch.setattr(tick, "_claimable", claimable_in)
    monkeypatch.setattr(tick, "_end_awaiting_seats", ended)
    monkeypatch.setattr(tick.freeze, "fix_phase", lambda config: "fix")
    monkeypatch.setattr(tick.freeze, "notice", lambda doc, rows, fix: f"drained {fix} {len(list(rows))}")
    return calls


@pytest.mark.feature("swarm-tick-territory")
def test_a_stopping_swarm_ends_awaiting_seats_and_stops_once_no_agent_is_left(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _settle_seams(monkeypatch)
    store = _SettleStore([_agent("engineer@1-1", "eng", "awaiting-decision")])
    config = SimpleNamespace(state="stopping")
    assert _settled(config, store, _Ledger(), {}, {}) == ["stopped"]
    assert calls == [("end", ["engineer@1-1"])]
    assert store.updates == [("sw", {"state": "stopped"})]


@pytest.mark.feature("swarm-tick-territory")
def test_a_stopping_swarm_waits_for_a_live_master_or_a_working_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _settle_seams(monkeypatch)
    config = SimpleNamespace(state="stopping")
    master = _SettleStore([_agent("master@1-1", "master")])
    assert _settled(config, master, _Ledger(), {}, {}) == []
    assert calls == []
    finished = _SettleStore([_agent("master@1-1", "master", "finished"), _agent("engineer@1-1", "eng")])
    assert _settled(config, finished, _Ledger(), {}, {}) == []
    assert calls == [("end", ["master@1-1", "engineer@1-1"])]
    assert master.updates == [] and finished.updates == []


@pytest.mark.feature("swarm-tick-territory")
def test_a_stopping_swarm_retires_agents_whose_runtime_never_answers_then_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _settle_seams(monkeypatch)
    dropped: list[tuple[str, str]] = []

    ghosts = [_agent("master@1-1", "master", tick.SUSPECT), _agent("engineer@1-1", "eng", tick.SUSPECT)]
    store, ledger, rows = _SettleStore(ghosts), _Ledger(), {"t1": {"state": "claimed"}}

    def drop(slug: str, seen: object, read: object, rows_seen: object, agent: AgentRecord, reason: str) -> str:
        assert (slug, seen, read, rows_seen) == ("sw", store, ledger, rows)
        dropped.append((agent.name, reason))
        return f"retired {agent.name}: {reason}"

    monkeypatch.setattr(tick, "_drop_remote", drop)
    config = SimpleNamespace(state="stopping")

    assert _settled(config, store, ledger, rows, {}) == [
        "retired master@1-1: the swarm is stopping and its runtime never answered",
        "retired engineer@1-1: the swarm is stopping and its runtime never answered",
        "stopped",
    ]
    assert dropped == [
        ("master@1-1", "the swarm is stopping and its runtime never answered"),
        ("engineer@1-1", "the swarm is stopping and its runtime never answered"),
    ]
    assert calls == []
    assert store.updates == [("sw", {"state": "stopped"})]


@pytest.mark.feature("swarm-tick-territory")
def test_a_stopping_swarm_keeps_a_ghost_while_another_agent_still_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    _settle_seams(monkeypatch)
    dropped: list[str] = []
    monkeypatch.setattr(tick, "_drop_remote", lambda *args: dropped.append(args[4].name) or "")
    store = _SettleStore([_agent("engineer@1-1", "eng", tick.SUSPECT), _agent("engineer@1-2", "eng")])

    assert _settled(SimpleNamespace(state="stopping"), store, _Ledger(), {}, {}) == []
    assert dropped == [] and store.updates == []


@pytest.mark.feature("swarm-tick-territory")
@pytest.mark.parametrize(
    ("blocked", "tail"),
    [(0, ""), (1, ", one blocked task waits for you"), (2, ", 2 blocked tasks wait for you")],
)
def test_a_running_swarm_with_nothing_claimable_drains_and_counts_blocked_tasks(
    monkeypatch: pytest.MonkeyPatch, blocked: int, tail: str
) -> None:
    calls = _settle_seams(monkeypatch)
    rows: dict[str, dict[str, Any]] = {f"b{i}": {"state": "blocked"} for i in range(blocked)}
    rows["x"] = {"state": "blocked", "out_of_scope": True}
    rows["d"] = {"state": "done"}
    store, ledger = _SettleStore([_agent("master@1-1", "master")]), _Ledger()
    assert _settled(SimpleNamespace(state="running"), store, ledger, rows, {}) == ["drained"]
    assert calls == [("claimable", "eng"), ("claimable", "ci"), ("claimable", "plan")]
    assert store.updates == [("sw", {"state": "drained"})]
    assert ledger.notices == [("sw", f"drained fix {len(rows)}{tail}")]


@pytest.mark.feature("swarm-tick-territory")
def test_a_swarm_with_work_or_workers_or_not_running_does_not_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _settle_seams(monkeypatch, claimable=True)
    master = [_agent("master@1-1", "master")]
    assert _settled(SimpleNamespace(state="running"), _SettleStore(master), _Ledger(), {}, {}) == []
    assert calls == [("claimable", "eng"), ("claimable", "ci")]
    worker = _SettleStore([*master, _agent("engineer@1-1", "eng")])
    assert _settled(SimpleNamespace(state="running"), worker, _Ledger(), {}, {}) == []
    paused = _SettleStore(master)
    assert _settled(SimpleNamespace(state="paused"), paused, _Ledger(), {}, {}) == []
    assert len(calls) == 2
    assert worker.updates == [] and paused.updates == []


@pytest.mark.feature("swarm-tick-territory")
def test_settle_hands_its_own_slug_store_ledger_rows_doc_and_config_to_each_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[Any, ...]] = []
    monkeypatch.setattr(tick, "_claimable", lambda *args: seen.append(("claimable", *args)) or [])
    monkeypatch.setattr(tick, "_end_awaiting_seats", lambda *args: seen.append(("end", *args)))
    monkeypatch.setattr(tick.freeze, "fix_phase", lambda config: seen.append(("fix", config)) or "fix")
    monkeypatch.setattr(tick.freeze, "notice", lambda doc, rows, fix: seen.append(("notice", doc, fix)) or "n")
    store, ledger = _SettleStore([_agent("master@1-1", "master", "finished")]), _Ledger()
    rows: dict[str, dict[str, Any]] = {"t1": {"state": "done"}}
    doc: dict[str, Any] = {"doc": 1}
    running, stopping = SimpleNamespace(state="running"), SimpleNamespace(state="stopping")
    assert _settled(running, store, ledger, rows, doc) == ["drained"]
    assert seen == [
        *(("claimable", "sw", store, rows, doc, lane) for lane in ("eng", "ci", "plan")),
        ("fix", running),
        ("notice", doc, "fix"),
    ]
    seen.clear()
    assert _settled(stopping, store, ledger, rows, doc) == []
    assert seen == [("end", "sw", store, ledger, rows, store.held)]


@pytest.mark.feature("swarm-tick-territory")
def test_sharing_names_each_running_task_with_a_shared_area_and_its_claimant(monkeypatch: pytest.MonkeyPatch) -> None:
    ended: list[object] = []

    def ended_agent(agent: Any, rows: object) -> bool:
        ended.append(rows)
        return agent.name == "engineer@1-9"

    monkeypatch.setattr(tick, "_ended", ended_agent)
    rows = {
        "t1": {"id": "t1", "state": "claimed", "territory": ["scripts/swarm"]},
        "t2": {"id": "t2", "state": "claimed", "territory": ["scripts/swarm/tick.py"]},
        "t3": {"id": "t3", "state": "pr", "territory": ["scripts/swarm/store.py"]},
        "t4": {"id": "t4", "state": "open", "territory": ["scripts/swarm"]},
        "t5": {"id": "t5", "state": "claimed", "territory": ["docs"]},
        "t6": {"id": "t6", "state": "pr", "territory": ["scripts"]},
    }
    agents = [
        SimpleNamespace(task="t2", name="engineer@1-2"),
        SimpleNamespace(task="t6", name="engineer@1-6"),
        SimpleNamespace(task="t3", name="engineer@1-9"),
    ]
    assert territory._sharing(rows["t1"], rows, agents) == [
        {"task": "t2", "claimant": "engineer@1-2", "areas": ["scripts/swarm/tick.py"]},
        {"task": "t3", "claimant": "", "areas": ["scripts/swarm/store.py"], "checks": True},
        {"task": "t6", "claimant": "engineer@1-6", "areas": ["scripts/swarm"]},
    ]
    assert territory._sharing({"id": "t9"}, rows, agents) == []
    assert len(ended) == 6 and all(seen is rows for seen in ended)


@pytest.mark.feature("swarm-tick-territory")
def test_stack_base_stacks_each_unfinished_dependency_with_the_given_or_default_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stacked: list[tuple[Any, ...]] = []

    def stacked_dep(dep: str, row: dict[str, Any], repo: str, origin: object) -> dict[str, str]:
        stacked.append((dep, row["branch"], repo, origin))
        return {"task": dep}

    def mine(repo: str) -> str:
        return repo

    monkeypatch.setattr(tick, "_stacked_dep", stacked_dep)
    rows = {"a": {"state": "done", "branch": "ba"}, "b": {"state": "pr", "branch": "bb"}, "c": {"branch": "bc"}}
    task = {"depends_on": ["a", "b", "c"]}
    assert territory._stack_base(task, rows, "repo", mine) == [{"task": "b"}, {"task": "c"}]
    assert stacked == [("b", "bb", "repo", mine), ("c", "bc", "repo", mine)]
    stacked.clear()
    assert territory._stack_base({"depends_on": ["b"]}, rows, "repo") == [{"task": "b"}]
    assert stacked == [("b", "bb", "repo", stack.origin_key)]
    assert territory._stack_base({}, rows, "repo") == []


@pytest.mark.feature("swarm-tick-territory")
def test_end_awaiting_seats_drops_only_agents_awaiting_a_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    dropped: list[tuple[Any, ...]] = []

    def drop(slug: str, store: object, ledger: object, rows: object, agent: AgentRecord) -> None:
        dropped.append((slug, store, ledger, rows, agent.name))

    monkeypatch.setattr(tick, "_drop", drop)
    store: Any = "store"
    rows: dict[str, dict[str, Any]] = {"t1": {}}
    agents = [_agent("engineer@1-1", "eng", "awaiting-decision"), _agent("engineer@1-2", "eng")]
    territory._end_awaiting_seats("sw", store, "ledger", rows, agents)
    assert dropped == [("sw", "store", "ledger", rows, "engineer@1-1")]


class _BinLedger:
    """A ledger answering whether a closed ledger moved to the bin and recording each ask."""

    def __init__(self, moved: bool) -> None:
        self.moved = moved
        self.asked: list[tuple[str, int]] = []

    def bin_closed(self, slug: str, closed_at: int) -> bool:
        self.asked.append((slug, closed_at))
        return self.moved


@pytest.mark.feature("swarm-tick-territory")
@pytest.mark.parametrize(
    ("closed_at", "agents", "moved", "actions", "closed"),
    [
        (5, [], True, ["the closed ledger moved to the bin"], True),
        (5, [], False, [], True),
        (5, ["engineer@1-1"], True, [], False),
        (0, [], True, [], False),
    ],
)
def test_bin_closed_closes_the_swarm_inbox_and_bins_a_closed_empty_ledger(
    monkeypatch: pytest.MonkeyPatch, closed_at: int, agents: list[str], moved: bool, actions: list[str], closed: bool
) -> None:
    swarms: list[tuple[object, str]] = []
    monkeypatch.setattr(tick, "InboxStore", lambda redis: ("inbox", redis))
    monkeypatch.setattr(tick.exits, "close_swarm", lambda inbox, slug: swarms.append((inbox, slug)))
    store = SimpleNamespace(redis="redis", agents=lambda slug: agents if slug == "sw" else ["other"])
    ledger = _BinLedger(moved)
    assert territory._bin_closed("sw", store, ledger, {"closed_at": closed_at}) == actions
    assert swarms == ([(("inbox", "redis"), "sw")] if closed else [])
    assert ledger.asked == ([("sw", closed_at)] if closed else [])
