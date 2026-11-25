"""The live check of the saved expert missions over a stand-in store."""

import hashlib
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.feature_checks import expert_missions as check
from scripts.swarm import expert_lineage
from tests.swarm.test_expert_mission import GOOD

pytestmark = pytest.mark.unit

DIGEST = hashlib.sha256(GOOD.encode()).hexdigest()
SOURCE = "/missions/batch.md"


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


def test_a_fleet_with_no_running_lineage_passes_and_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(("rig",))
    assert check.main(store) == 0
    assert capsys.readouterr().out == "no running expert lineage\n"
    ended = expert_lineage.start(store, "rig", GOOD, SOURCE, "xhigh", "operator", 1)
    expert_lineage.stop(store, "rig", ended.id, "operator", "superseded", 2)
    store.redis.hdel(store.key("rig", "expert-missions"), DIGEST)
    assert check.main(store) == 0
    assert capsys.readouterr().out == "no running expert lineage\n"


@pytest.mark.feature("expert-mission-and-lineage")
def test_each_running_lineage_whose_mission_is_saved_passes_and_is_listed(capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(("rig", "old", "doctor"), stopped=("old",))
    expert_lineage.start(store, "rig", GOOD, SOURCE, "max", "operator", 1)
    second = expert_lineage.start(store, "rig", GOOD, SOURCE, "xhigh", "operator", 2)
    expert_lineage.stop(store, "rig", second.id, "operator", "superseded", 3)
    expert_lineage.start(store, "rig", GOOD, SOURCE, "xhigh", "operator", 4)
    expert_lineage.start(store, "doctor", GOOD, SOURCE, "xhigh", "operator", 1)
    expert_lineage.start(store, "old", GOOD, SOURCE, "xhigh", "operator", 1)
    store.redis.hdel(store.key("old", "expert-missions"), DIGEST)
    assert check.main(store) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"doctor lineage expert-1 runs at xhigh with its mission saved as mission:doctor/{DIGEST}",
        f"rig lineage expert-1 runs at max with its mission saved as mission:rig/{DIGEST}",
        f"rig lineage expert-3 runs at xhigh with its mission saved as mission:rig/{DIGEST}",
    ]


@pytest.mark.feature("expert-mission-and-lineage")
@pytest.mark.parametrize("saved", ["another text", None])
def test_a_running_lineage_whose_saved_mission_is_gone_or_changed_fails_by_name(
    capsys: pytest.CaptureFixture[str], saved: str | None
) -> None:
    store = Store(("rig",))
    later = GOOD + "\nA second mission.\n"
    expert_lineage.start(store, "rig", GOOD, SOURCE, "xhigh", "operator", 1)
    expert_lineage.start(store, "rig", later, "/missions/next.md", "xhigh", "operator", 2)
    if saved is None:
        store.redis.hdel(store.key("rig", "expert-missions"), DIGEST)
    else:
        store.redis.hset(store.key("rig", "expert-missions"), DIGEST, saved)
    assert check.main(store) == 1
    assert capsys.readouterr().out.splitlines() == [
        "FAIL: rig lineage expert-1: the saved mission of lineage expert-1 is missing or does not match "
        f"mission:rig/{DIGEST}",
        "rig lineage expert-2 runs at xhigh with its mission saved as "
        f"mission:rig/{hashlib.sha256(later.encode()).hexdigest()}",
    ]


def test_with_no_store_given_the_check_reads_the_swarm_store(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm import store as swarm_store

    client = object()
    found = Store(("rig",))
    expert_lineage.start(found, "rig", GOOD, SOURCE, "max", "operator", 1)
    monkeypatch.setattr(swarm_store, "redis_client", lambda: client)
    monkeypatch.setattr(swarm_store, "RedisStore", lambda redis: found if redis is client else None)
    assert check.main() == 0
    assert capsys.readouterr().out == (
        f"rig lineage expert-1 runs at max with its mission saved as mission:rig/{DIGEST}\n"
    )
