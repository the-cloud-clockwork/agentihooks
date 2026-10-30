import pytest

from scripts.swarm import blockers
from scripts.swarm.health import spawn_stall
from scripts.swarm.health.findings import Finding
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick_watch import STALE_TICK, _alert

pytestmark = pytest.mark.unit

SLUG = "sw"
HOUR = blockers.SUSTAINED_MS
RECENT = blockers.RECENT_MS
DAY = blockers.TTL_MS


@pytest.fixture
def store():
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig(SLUG, "/repo", max_eng=1, max_ci=1))
    return found


def test_record_tracks_first_seen_last_seen_and_count(store):
    blockers.record(store, SLUG, "cat", "first", 1_000)
    blockers.record(store, SLUG, "cat", "second", 5_000)
    found = blockers.active(store, SLUG, 5_000)
    assert found["cat"] == blockers.Blocker("cat", 1_000, 5_000, 2, "second")


def test_entries_expire_at_the_ttl(store):
    blockers.record(store, SLUG, "cat", "detail", 0)
    assert "cat" in blockers.active(store, SLUG, DAY - 1)
    assert "cat" not in blockers.active(store, SLUG, DAY)


def test_sustained_boundary_is_at_one_hour(store):
    blockers.record(store, SLUG, "cat", "detail", 0)
    blockers.record(store, SLUG, "cat", "detail", HOUR - 1)
    assert "cat" not in blockers.sustained(store, SLUG, HOUR - 1)
    blockers.record(store, SLUG, "cat", "detail", HOUR)
    assert "cat" in blockers.sustained(store, SLUG, HOUR)


def test_a_category_that_stops_recurring_ages_out_of_sustained(store):
    blockers.record(store, SLUG, "cat", "detail", 0)
    blockers.record(store, SLUG, "cat", "detail", HOUR)
    assert "cat" in blockers.sustained(store, SLUG, HOUR + RECENT)
    assert "cat" not in blockers.sustained(store, SLUG, HOUR + RECENT + 1)
    assert "cat" in blockers.active(store, SLUG, HOUR + RECENT + 1)


def test_clear_removes_the_category(store):
    blockers.record(store, SLUG, "cat", "detail", 0)
    blockers.clear(store, SLUG, "cat")
    assert blockers.active(store, SLUG, 0) == {}


def test_due_notice_fires_once_per_notice_window(store):
    assert blockers.due_notice(store, SLUG, 0) is True
    assert blockers.due_notice(store, SLUG, 1) is False
    assert blockers.due_notice(store, SLUG, blockers.NOTICE_MS) is True


def test_a_tick_pass_failure_is_recorded_by_category(store, monkeypatch):
    from scripts.swarm import timing

    clock = [1_000]
    monkeypatch.setattr(spawn_stall, "observe", lambda *args: {})
    monkeypatch.setattr(spawn_stall, "deliver", lambda *args: None)
    monkeypatch.setattr(spawn_stall, "findings", lambda store, slug: [])
    with pytest.raises(RuntimeError, match="^kaboom$"):
        with timing.tick(SLUG), spawn_stall.watch(store, SLUG, _Ledger(), lambda: clock[0], _Runtime()):
            with timing.step("boom"):
                raise RuntimeError("kaboom")
    found = blockers.active(store, SLUG, clock[0])
    assert found["RuntimeError"].detail == "boom: kaboom"


def test_a_spawn_stall_finding_is_recorded_as_spawn_stall(store, monkeypatch):
    clock = [1_000]
    finding = Finding("spawn stall", SLUG, "No agent has launched for ten minutes", (), "ten minutes", 1)
    monkeypatch.setattr(spawn_stall, "findings", lambda store, slug: [finding])
    monkeypatch.setattr(spawn_stall, "observe", lambda *args: {})
    monkeypatch.setattr(spawn_stall, "deliver", lambda *args: None)
    with spawn_stall.watch(store, SLUG, _Ledger(), lambda: clock[0], _Runtime()):
        pass
    found = blockers.active(store, SLUG, clock[0])
    assert found["spawn stall"].detail == "No agent has launched for ten minutes"


def test_a_stale_tick_is_recorded_once_per_alert(store):
    _alert(store, SLUG, 10_000, None, ["no tick pass has completed for 10 minutes"])
    found = blockers.active(store, SLUG, 10_000)
    assert found[STALE_TICK].last_seen == 10_000


class _Ledger:
    def state(self, slug):
        return {"tasks": []}


class _Runtime:
    def quota_requirements(self, *args):
        return None
