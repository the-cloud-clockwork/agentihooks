from dataclasses import asdict
from typing import Any

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def store() -> Any:
    import fakeredis

    from scripts.swarm.store import RedisStore

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    from scripts.swarm import fencing, lease

    at = [1000]
    monkeypatch.setattr(fencing, "now_ms", lambda: at[0])
    monkeypatch.setattr(lease, "now_ms", lambda _store: at[0])
    return at


def _records(store: Any) -> None:
    from scripts.swarm import fencing

    fencing.migrate_records(store, "sw", writers_upgraded=True)


def _counted_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from scripts.swarm import fencing

    real = fencing.write
    calls: list[str] = []

    def counted(store: Any, slug: str, change: Any, fence: Any = None) -> Any:
        calls.append(slug)
        return real(store, slug, change, fence)

    monkeypatch.setattr(fencing, "write", counted)
    return calls


@pytest.mark.feature("lease-cache-records")
def test_the_cheap_lease_read_answers_the_lease_written_through_records(store: Any, clock: list[int]) -> None:
    from scripts.swarm import fencing, lease

    _records(store)
    held = lease.acquire(store, "sw", "home")
    assert held is not None
    assert fencing.read_lease(store, "sw") == {"lease": asdict(held)}

    clock[0] = held.expires_at + 1
    taken = lease.acquire(store, "sw", "other")
    assert taken is not None
    assert taken.epoch == held.epoch + 1
    assert fencing.read_lease(store, "sw") == {"lease": asdict(taken)}


@pytest.mark.feature("lease-cache-records")
def test_the_cheap_lease_read_survives_an_unrelated_records_write(store: Any, clock: list[int]) -> None:
    from scripts.swarm import fencing, lease

    _records(store)
    held = lease.acquire(store, "sw", "home")
    assert held is not None
    fencing.write(store, "sw", lambda doc: doc["grants"].update({"g": {"n": 1}}))
    assert fencing.read_lease(store, "sw") == {"lease": asdict(held)}


@pytest.mark.feature("lease-cache-records")
def test_a_renew_on_records_inside_its_margin_writes_nothing(
    store: Any, clock: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import lease

    _records(store)
    held = lease.acquire(store, "sw", "home")
    assert held is not None
    calls = _counted_writes(monkeypatch)
    assert lease.renew(store, "sw", held) == held
    assert lease.current(store, "sw") == held
    assert calls == []


@pytest.mark.feature("lease-cache-records")
def test_a_renew_on_records_refuses_a_lease_another_owner_took(store: Any, clock: list[int]) -> None:
    from scripts.swarm import lease
    from scripts.swarm.store import SwarmError

    _records(store)
    held = lease.acquire(store, "sw", "home")
    assert held is not None
    clock[0] = held.expires_at + 1
    assert lease.acquire(store, "sw", "other") is not None
    with pytest.raises(SwarmError, match=f"^{lease.STALE}$"):
        lease.renew(store, "sw", held)


@pytest.mark.feature("lease-cache-records")
def test_a_records_swarm_with_no_lease_reads_none(store: Any) -> None:
    from scripts.swarm import fencing

    _records(store)
    assert fencing.read_lease(store, "sw") == {"lease": None}


@pytest.mark.feature("lease-cache-records")
def test_a_document_swarm_still_reads_its_lease_from_the_cache(store: Any, clock: list[int]) -> None:
    from scripts.swarm import fencing, lease

    held = lease.acquire(store, "sw", "home")
    assert held is not None
    assert fencing.records(store, "sw").layout("sw") == fencing.DOCUMENT
    assert fencing.read_lease(store, "sw") == {"lease": asdict(held)}
    store.redis.delete(store.key("sw", fencing.LEASE_CACHE))
    assert fencing.read_lease(store, "sw") is None


def test_a_fence_key_matches_the_records_port_key_without_registering_its_script(store: Any) -> None:
    from scripts.swarm.store import PREFIX
    from scripts.tick.adapters import records_redis

    assert records_redis.fence_key("sw", "meta") == f"{PREFIX}:{{sw}}:fence:meta"
    assert records_redis.fence_key("sw", "meta", "root") == "root:{sw}:fence:meta"
    assert records_redis.fence_key("sw", "meta") == records_redis.RedisRecords(store.redis).key("sw", "meta")
