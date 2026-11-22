"""The live check of the expert handoffs over a stand-in store."""

from types import SimpleNamespace
from typing import Any

import pytest

from scripts.feature_checks import expert_handoffs as check
from scripts.swarm import expert_lineage
from tests.handoff.test_expert_contract import ADDRESS, LINE, OPENS, VALID
from tests.swarm.test_expert_handoff_command import V2
from tests.swarm.test_expert_mission import GOOD

pytestmark = pytest.mark.unit

SOURCE = "/missions/batch.md"
DIGEST = ADDRESS.rpartition("/")[2]
RECOVERY = "## Intent\nContinue the mission; the last expert left no handoff.\n\n<!-- handoff complete -->\n"


class Store:
    def __init__(self, swarms: tuple[str, ...], stopped: tuple[str, ...] = ()) -> None:
        import fakeredis

        self.swarms, self.stopped = swarms, stopped
        self.redis: Any = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
        self.documents: dict[tuple[str, str], str] = {}
        self.envelopes: dict[tuple[str, str], dict[str, Any]] = {}

    def slugs(self) -> list[str]:
        return list(self.swarms)

    def config(self, slug: str) -> SimpleNamespace:
        return SimpleNamespace(state="stopped" if slug in self.stopped else "running")

    def key(self, slug: str, *parts: str) -> str:
        return ":".join(("fleet", slug, *parts))

    def handoff(self, slug: str, task: str) -> str:
        return self.documents.get((slug, task), "")

    def handoff_envelope(self, slug: str, task: str) -> dict[str, Any] | None:
        return self.envelopes.get((slug, task))


def _started(store: Store, slug: str, count: int = 1) -> list[expert_lineage.Lineage]:
    return [expert_lineage.start(store, slug, GOOD, SOURCE, "xhigh", "operator", at) for at in range(1, count + 1)]


def _for(slug: str) -> str:
    return VALID.replace("mission:sw/", f"mission:{slug}/")


def test_a_fleet_whose_running_lineages_hold_no_handoff_passes_and_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(("rig",))
    assert check.main(store) == 0
    _started(store, "rig")
    assert check.main(store) == 0
    assert capsys.readouterr().out == "no running expert lineage holds a handoff\n" * 2


@pytest.mark.feature("expert-handoff-contract")
def test_each_conforming_handoff_of_a_running_lineage_passes_and_is_listed(capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(("rig", "old", "doctor"), stopped=("old",))
    _started(store, "rig", 4)
    _started(store, "doctor")
    _started(store, "old")
    expert_lineage.stop(store, "rig", "expert-2", "operator", "superseded", 9)
    store.documents = {
        ("rig", "expert-1"): _for("rig"),
        ("rig", "expert-2"): V2,
        ("rig", "expert-3"): RECOVERY,
        ("doctor", "expert-1"): "\n" + _for("doctor"),
        ("old", "expert-1"): V2,
    }
    assert check.main(store) == 0
    assert capsys.readouterr().out.splitlines() == [
        "doctor lineage expert-1 holds a conforming expert handoff",
        "rig lineage expert-1 holds a conforming expert handoff",
        "rig lineage expert-3 holds a recovery note the runtime wrote",
    ]


@pytest.mark.feature("expert-handoff-contract")
def test_a_handoff_of_a_running_lineage_that_breaks_the_contract_fails_with_each_fault(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = Store(("rig",))
    _started(store, "rig", 3)
    wrong = OPENS.replace(LINE, LINE.replace("mission:sw/", "mission:rig/"))
    store.documents = {
        ("rig", "expert-1"): _for("rig"),
        ("rig", "expert-2"): VALID.replace("- D3 The implementation, nothing merged\n", ""),
        ("rig", "expert-3"): _for("rig").replace("# Expert handoff", "# Handoff v2"),
    }
    assert check.main(store) == 1
    assert capsys.readouterr().out.splitlines() == [
        "rig lineage expert-1 holds a conforming expert handoff",
        f"FAIL: rig lineage expert-2: {wrong}; deliverable D3 is named by no Done or Pending bullet; start one with D3",
        "FAIL: rig lineage expert-3: the first line must be # Expert handoff",
    ]


@pytest.mark.feature("expert-seat-step")
def test_the_handoff_a_lineage_was_started_from_is_held_to_handoff_v2(capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(("rig",))
    _started(store, "rig", 3)
    store.documents = {
        ("rig", "expert-1"): V2,
        ("rig", "expert-2"): V2.replace("## Intent\n", "## Intend\n"),
        ("rig", "expert-3"): V2,
    }
    store.envelopes = {("rig", "expert-1"): {"reason": "operator"}, ("rig", "expert-2"): {"reason": "operator"}}
    assert check.main(store) == 1
    seeded, faulty, unseeded = capsys.readouterr().out.splitlines()
    assert seeded == "rig lineage expert-1 holds the conforming Handoff v2 it was started from"
    assert faulty == (
        "FAIL: rig lineage expert-2: ## Intend is not one of the six headings: Intent, Done, Stopped at, Decisions "
        "and promises, Next, Read first; ## Intent is missing"
    )
    assert unseeded.startswith("FAIL: rig lineage expert-3: the first line must be # Expert handoff; ")


@pytest.mark.feature("expert-handoff-contract")
def test_a_handoff_whose_saved_mission_is_gone_fails_by_name(capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(("rig",))
    _started(store, "rig")
    store.documents = {("rig", "expert-1"): _for("rig")}
    store.redis.hdel(store.key("rig", "expert-missions"), DIGEST)
    assert check.main(store) == 1
    assert capsys.readouterr().out == (
        "FAIL: rig lineage expert-1: the saved mission of lineage expert-1 is missing or does not match "
        f"mission:rig/{DIGEST}\n"
    )


def test_with_no_store_given_the_check_reads_the_swarm_store(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm import store as swarm_store

    client = object()
    found = Store(("rig",))
    _started(found, "rig")
    found.documents = {("rig", "expert-1"): _for("rig")}
    monkeypatch.setattr(swarm_store, "redis_client", lambda: client)
    monkeypatch.setattr(swarm_store, "RedisStore", lambda redis: found if redis is client else None)
    assert check.main() == 0
    assert capsys.readouterr().out == "rig lineage expert-1 holds a conforming expert handoff\n"
