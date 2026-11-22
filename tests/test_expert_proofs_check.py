"""The live check of the mission proofs over a stand-in store."""

from types import SimpleNamespace
from typing import Any

import pytest

from scripts.feature_checks import expert_proofs as check
from scripts.swarm import expert_lineage
from tests.handoff.test_mission_proof import D3, HEADINGS, VALID
from tests.swarm.test_expert_mission import GOOD

pytestmark = pytest.mark.unit

SOURCE = "/missions/batch.md"
EXPERT = "expert@a1b2c3-0001"


class Store:
    def __init__(self, swarms: tuple[str, ...], stopped: tuple[str, ...] = ()) -> None:
        import fakeredis

        self.swarms, self.stopped = swarms, stopped
        self.redis: Any = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)

    def slugs(self) -> list[str]:
        return list(self.swarms)

    def config(self, slug: str) -> SimpleNamespace:
        return SimpleNamespace(state="stopped" if slug in self.stopped else "running")

    def key(self, slug: str, *parts: str) -> str:
        return ":".join(("fleet", slug, *parts))


def _for(slug: str, text: str = VALID) -> str:
    return text.replace("mission:sw/", f"mission:{slug}/")


def _ended(store: Store, slug: str, proofs: list[str | None]) -> None:
    """Start one lineage per entry and complete each one whose entry is a proof text."""
    for at, proof in enumerate(proofs, 1):
        lineage = expert_lineage.start(store, slug, GOOD, SOURCE, "xhigh", "operator", at)
        if proof is not None:
            expert_lineage.complete(store, slug, lineage.id, EXPERT, proof, at + 100)


def test_a_fleet_with_no_completed_lineage_passes_and_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(("rig",))
    assert check.main(store) == 0
    _ended(store, "rig", [None])
    expert_lineage.stop(store, "rig", "expert-1", "operator", "the order changed", 9)
    assert check.main(store) == 0
    assert capsys.readouterr().out == "no expert lineage has completed\n" * 2


@pytest.mark.feature("expert-lineage-ends-on-proof")
def test_each_completed_lineage_whose_proof_conforms_passes_and_is_listed(capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(("rig", "old", "doctor"), stopped=("old",))
    _ended(store, "rig", [_for("rig"), None, _for("rig")])
    _ended(store, "doctor", [_for("doctor")])
    _ended(store, "old", ["not a proof"])
    assert check.main(store) == 0
    assert capsys.readouterr().out.splitlines() == [
        "doctor lineage expert-1 ended on a mission proof that conforms",
        "rig lineage expert-1 ended on a mission proof that conforms",
        "rig lineage expert-3 ended on a mission proof that conforms",
    ]


@pytest.mark.feature("expert-lineage-ends-on-proof")
def test_a_completed_lineage_whose_proof_is_gone_or_breaks_the_contract_fails_by_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = Store(("rig",))
    _ended(store, "rig", [_for("rig"), _for("rig", VALID.replace(D3, "")), _for("rig"), _for("rig")])
    store.redis.hdel(store.key("rig", "expert-proofs"), "expert-3")
    store.redis.hset(
        store.key("rig", "expert-proofs"), "expert-4", _for("rig").replace("None\n<!--", "- one left\n<!--")
    )
    assert check.main(store) == 1
    assert capsys.readouterr().out.splitlines() == [
        "rig lineage expert-1 ended on a mission proof that conforms",
        "FAIL: rig lineage expert-2: deliverable D3 is named by no Deliverables bullet; start one with D3",
        "FAIL: rig lineage expert-3 reads complete and holds no mission proof",
        "FAIL: rig lineage expert-4: ## Open items must be None; a lineage ends with nothing open",
    ]


def test_a_kept_proof_with_two_faults_names_both_on_its_line(capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(("rig",))
    _ended(store, "rig", [_for("rig")])
    faulty = _for("rig").replace(
        "## Open items\nNone\n", "## Read first\n- recap:expert-1@rig what was done\n## Open items\n- one left\n"
    )
    store.redis.hset(store.key("rig", "expert-proofs"), "expert-1", faulty)
    assert check.main(store) == 1
    assert capsys.readouterr().out == (
        f"FAIL: rig lineage expert-1: ## Read first is not one of the eight headings: {HEADINGS}; "
        "## Open items must be None; a lineage ends with nothing open\n"
    )


def test_a_completed_lineage_whose_saved_mission_is_gone_fails_by_name(capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(("rig",))
    _ended(store, "rig", [_for("rig")])
    digest = expert_lineage.get(store, "rig", "expert-1").mission.rpartition("/")[2]
    store.redis.hdel(store.key("rig", "expert-missions"), digest)
    assert check.main(store) == 1
    assert capsys.readouterr().out == (
        "FAIL: rig lineage expert-1: the saved mission of lineage expert-1 is missing or does not match "
        f"mission:rig/{digest}\n"
    )


def test_every_lineage_is_read_by_its_id_in_the_order_started_and_a_lost_record_is_left_out() -> None:
    store = Store(("rig",))
    assert expert_lineage.every(store, "rig") == []
    _ended(store, "rig", [None] * 11)
    assert [row.id for row in expert_lineage.every(store, "rig")] == [f"expert-{n}" for n in range(1, 12)]
    store.redis.hdel(store.key("rig", "expert-lineages"), "expert-4")
    expert_lineage.stop(store, "rig", "expert-2", "operator", "superseded", 50)
    found = expert_lineage.every(store, "rig")
    assert [row.id for row in found] == [f"expert-{n}" for n in (1, 2, 3, 5, 6, 7, 8, 9, 10, 11)]
    assert [row.state for row in found][:3] == ["running", "stopped", "running"]
    assert expert_lineage.every(store, "other") == []


def test_with_no_store_given_the_check_reads_the_swarm_store(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm import store as swarm_store

    client = object()
    found = Store(("rig",))
    _ended(found, "rig", [_for("rig")])
    monkeypatch.setattr(swarm_store, "redis_client", lambda: client)
    monkeypatch.setattr(swarm_store, "RedisStore", lambda redis: found if redis is client else None)
    assert check.main() == 0
    assert capsys.readouterr().out == "rig lineage expert-1 ended on a mission proof that conforms\n"
