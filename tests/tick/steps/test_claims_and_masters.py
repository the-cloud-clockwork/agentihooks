"""The claims cluster lives in the claims step module, the master operations in the masters step module, and the
swarm tick module re-exports each name."""

from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import stack, tick
from scripts.swarm import store as swarm_store
from scripts.swarm.store import AgentRecord
from scripts.tick.steps import claims, masters

pytestmark = pytest.mark.unit

CLAIMED = (
    "SpawnError",
    "Placed",
    "Ledger",
    "Runtime",
    "agent_status",
    "ready_in_order",
    "_owned_tasks",
    "_claimable",
    "Claims",
    "claim_view",
    "_launch_order",
    "_demoted",
    "_parked",
    "_stack_base",
    "_stacked_dep",
    "_refund_parked",
    "_master_set",
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
MASTERED = (
    "_master_down",
    "_woken",
    "_recover_master",
    "_night_watch",
    "_master",
    "_retire_master",
    "respawn_master",
    "place_master",
    "_under_tick_lock",
    "_PassView",
    "_run_group",
    "_print_actions",
    "_codex_hook_order",
)
MOVED = [(claims, name) for name in CLAIMED] + [(masters, name) for name in MASTERED]


@pytest.mark.feature("swarm-tick-claims-and-masters")
@pytest.mark.parametrize(("module", "name"), MOVED)
def test_tick_reexports_the_moved_name(module: ModuleType, name: str) -> None:
    moved = getattr(module, name)
    assert moved.__module__ == module.__name__
    assert getattr(tick, name.removeprefix("_") if name == "_run_group" else name) is moved


@pytest.mark.feature("swarm-tick-claims-and-masters")
def test_the_tick_module_holds_no_moved_definition() -> None:
    text = Path(str(tick.__file__)).read_text(encoding="utf-8")
    held = [name for _, name in MOVED if f"\ndef {name}(" in text or f"\nclass {name}" in text]
    assert held == []


@pytest.mark.feature("swarm-tick-claims-and-masters")
def test_moved_claim_code_reaches_tick_seams_through_the_tick_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tick, "_shared", lambda mine, theirs: ["scripts"] if mine == ["a"] else [])
    assert claims._overlaps(["a"], ["b"]) is True
    assert claims._overlaps(["c"], ["b"]) is False


@pytest.mark.feature("swarm-tick-claims-and-masters")
def test_moved_master_code_reaches_tick_seams_through_the_tick_module(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Any, ...]] = []

    def locked(slug: str, store: Any, act: Any, caller: str) -> list[str]:
        seen.append((slug, store, caller))
        return ["locked"]

    monkeypatch.setattr(tick, "_under_tick_lock", locked)
    assert masters.place_master("sw", "store", "runtime", 1) == ["locked"]
    assert masters.respawn_master("sw", "store", "runtime", 1) == ["locked"]
    assert seen == [("sw", "store", "place master"), ("sw", "store", "respawn master")]


@pytest.mark.feature("swarm-tick-claims-and-masters")
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
    assert claims._shared(mine, theirs) == shared
    assert claims._overlaps(mine, theirs) is bool(shared)


@pytest.mark.feature("swarm-tick-claims-and-masters")
@pytest.mark.parametrize(
    ("fields", "status"),
    [
        ({"state": "finished"}, "finished"),
        ({"state": "awaiting-decision"}, "awaiting-decision"),
        ({"input_prompt": "y/n"}, "waiting"),
        ({"idle_ticks": 3}, "stalled"),
        ({"idle_ticks": 2}, "idle"),
        ({}, "working"),
    ],
)
def test_agent_status_reads_state_prompt_and_idle_ticks(fields: dict[str, Any], status: str) -> None:
    agent = AgentRecord("engineer@1-1", "eng", "t1", **fields)
    assert claims.agent_status(agent) == status


@pytest.mark.feature("swarm-tick-claims-and-masters")
def test_the_store_projects_agent_status_from_the_claims_module() -> None:
    text = Path(str(swarm_store.__file__)).read_text(encoding="utf-8")
    assert "from scripts.tick.steps.claims import agent_status" in text
    assert "from scripts.swarm.tick import agent_status" not in text


@pytest.mark.feature("swarm-tick-claims-and-masters")
def test_a_parked_task_waits_until_every_parked_dependency_is_done() -> None:
    rows = {"a": {"state": "done"}, "b": {"state": "claimed"}}
    assert claims._parked({"parked_on": ["a", "b"]}, rows) is True
    assert claims._parked({"parked_on": ["a"]}, rows) is False
    assert claims._parked({}, rows) is False


@pytest.mark.feature("swarm-tick-claims-and-masters")
def test_a_stacked_dependency_names_its_repo_only_when_it_lives_elsewhere() -> None:
    row = {"branch": "b1", "branch_repo": "https://example.test/other.git"}
    key = stack.repo_key(row["branch_repo"])
    assert claims._stacked_dep("d1", {"branch": "b1"}, "repo", lambda repo: "x") == {"task": "d1", "branch": "b1"}
    assert claims._stacked_dep("d1", row, "repo", lambda repo: key) == {"task": "d1", "branch": "b1"}
    elsewhere = {"task": "d1", "branch": "b1", "repo": "https://example.test/other.git"}
    assert claims._stacked_dep("d1", row, "repo", lambda repo: "local") == elsewhere


@pytest.mark.feature("swarm-tick-claims-and-masters")
def test_master_set_reads_the_state_stamp_author_lane() -> None:
    def doc(by: str) -> dict[str, Any]:
        return {"_meta": {"stamps": {"tasks/t1/state": {"by": by}}}}

    assert claims._master_set(doc("master@323133-6160"), "t1") == "master"
    assert claims._master_set(doc("engineer@323133-6160"), "t1") == ""
    assert claims._master_set({}, "t1") == ""


@pytest.mark.feature("swarm-tick-claims-and-masters")
def test_close_space_closes_only_an_empty_swarm() -> None:
    closed: list[object] = []
    runtime = SimpleNamespace(close_space=closed.append)
    claims._close_space("sw", "config", SimpleNamespace(agents=lambda slug: ["one"]), runtime)
    assert closed == []
    assert claims._close_space("sw", "config", SimpleNamespace(agents=lambda slug: []), runtime) == []
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


@pytest.mark.feature("swarm-tick-claims-and-masters")
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
    claims._refund_parked("sw", store, rows, agent)
    assert store.asked == [("sw", "t1")]
    assert taken == [(store, "sw", "engineer@1-1")]
    assert store.refunded == ([("sw", "t1", "engineer@1-1", 7)] if refunded else [])
