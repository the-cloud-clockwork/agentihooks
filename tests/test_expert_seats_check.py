"""The live check of the expert seats over a stand-in store."""

from types import SimpleNamespace

import pytest

from scripts.feature_checks import expert_seats as check
from scripts.swarm import expert_lineage
from tests.swarm.test_expert_mission import GOOD
from tests.test_expert_handoffs_check import Store as Documents

pytestmark = pytest.mark.unit


def started(store: "Store", slug: str, count: int = 1) -> list[expert_lineage.Lineage]:
    return [
        expert_lineage.start(store, slug, GOOD, "/missions/batch.md", "xhigh", "operator", at)
        for at in range(1, count + 1)
    ]


class Store(Documents):
    """The stand-in store of the handoffs check, with the agents each swarm lists."""

    def __init__(self, swarms: tuple[str, ...], stopped: tuple[str, ...] = ()) -> None:
        super().__init__(swarms, stopped)
        self.crew: dict[str, list[SimpleNamespace]] = {}

    def agents(self, slug: str) -> list[SimpleNamespace]:
        return self.crew.get(slug, [])


def agent(name: str, task: str, lane: str = "expert", state: str = "working") -> SimpleNamespace:
    return SimpleNamespace(name=name, lane=lane, task=task, state=state)


def test_a_fleet_with_no_running_lineage_passes_and_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    assert check.main(Store(("rig",))) == 0
    assert capsys.readouterr().out == "no running expert lineage\n"


@pytest.mark.feature("expert-seat-step")
def test_each_running_lineage_is_listed_with_the_one_expert_on_its_seat_or_none(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = Store(("rig", "old", "doctor"), stopped=("old",))
    started(store, "rig", 3)
    started(store, "doctor")
    started(store, "old")
    expert_lineage.stop(store, "rig", "expert-3", "operator", "superseded", 9)
    store.crew = {
        "rig": [
            agent("expert@rig-0001", "expert-1"),
            agent("expert@rig-0002", "expert-2", state="finished"),
            agent("expert@rig-0003", "expert-3"),
            agent("engineer@rig-0004", "t1", lane="eng"),
        ],
        "old": [agent("expert@old-0001", "expert-1"), agent("expert@old-0002", "expert-1")],
    }
    assert check.main(store) == 0
    assert capsys.readouterr().out.splitlines() == [
        "doctor lineage expert-1 has no expert on its seat",
        "rig lineage expert-1 is worked by expert@rig-0001",
        "rig lineage expert-2 has no expert on its seat",
    ]


@pytest.mark.feature("expert-seat-step")
def test_a_seat_two_experts_hold_and_an_expert_of_no_lineage_each_fail_by_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = Store(("rig",))
    started(store, "rig", 2)
    store.crew = {
        "rig": [
            agent("expert@rig-0002", "expert-1"),
            agent("expert@rig-0001", "expert-1"),
            agent("expert@rig-0003", "expert-9"),
        ]
    }
    assert check.main(store) == 1
    assert capsys.readouterr().out.splitlines() == [
        "FAIL: rig lineage expert-1 is held by 2 experts: expert@rig-0001, expert@rig-0002",
        "rig lineage expert-2 has no expert on its seat",
        "FAIL: rig expert expert@rig-0003 holds the seat of expert-9, which is no lineage of the swarm",
    ]


def test_with_no_store_given_the_check_reads_the_swarm_store(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm import store as swarm_store

    client = object()
    found = Store(("rig",))
    started(found, "rig")
    monkeypatch.setattr(swarm_store, "redis_client", lambda: client)
    monkeypatch.setattr(swarm_store, "RedisStore", lambda redis: found if redis is client else None)
    assert check.main() == 0
    assert capsys.readouterr().out == "rig lineage expert-1 has no expert on its seat\n"
