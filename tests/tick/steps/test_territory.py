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


@pytest.mark.feature("swarm-tick-territory")
def test_a_stacked_dependency_names_its_repo_only_when_it_lives_elsewhere() -> None:
    row = {"branch": "b1", "branch_repo": "https://example.test/other.git"}
    key = stack.repo_key(row["branch_repo"])
    assert territory._stacked_dep("d1", {"branch": "b1"}, "repo", lambda repo: "x") == {"task": "d1", "branch": "b1"}
    assert territory._stacked_dep("d1", row, "repo", lambda repo: key) == {"task": "d1", "branch": "b1"}
    elsewhere = {"task": "d1", "branch": "b1", "repo": "https://example.test/other.git"}
    assert territory._stacked_dep("d1", row, "repo", lambda repo: "local") == elsewhere


@pytest.mark.feature("swarm-tick-territory")
def test_master_set_reads_the_state_stamp_author_lane() -> None:
    def doc(by: str) -> dict[str, Any]:
        return {"_meta": {"stamps": {"tasks/t1/state": {"by": by}}}}

    assert territory._master_set(doc("master@323133-6160"), "t1") == "master"
    assert territory._master_set(doc("engineer@323133-6160"), "t1") == ""
    assert territory._master_set({}, "t1") == ""


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
