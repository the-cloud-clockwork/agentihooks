import json

import pytest

from scripts.swarm import blockers, cli
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


def test_sustained_uses_the_span_not_the_sum(store):
    blockers.record(store, SLUG, "cat", "detail", HOUR)
    blockers.record(store, SLUG, "cat", "detail", 2 * HOUR - 1)
    assert "cat" not in blockers.sustained(store, SLUG, 2 * HOUR - 1)


def test_key_combines_slug_and_bucket(store):
    assert blockers._key(store, SLUG) == store.key(SLUG, blockers.KEY)


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


def test_due_notice_is_scoped_per_slug(store):
    assert blockers.due_notice(store, SLUG, 0) is True
    assert blockers.due_notice(store, "other-slug", 0) is True


def test_active_keeps_checking_after_an_expired_entry(store):
    blockers.record(store, SLUG, "old", "d", 0)
    blockers.record(store, SLUG, "new", "d", blockers.TTL_MS - 1)
    found = blockers.active(store, SLUG, blockers.TTL_MS)
    assert "new" in found
    assert "old" not in found


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


def test_a_stale_tick_joins_evidence_with_a_semicolon(store):
    _alert(store, SLUG, 10_000, None, ["first", "second"])
    found = blockers.active(store, SLUG, 10_000)
    assert found[STALE_TICK].detail == "first; second"


def test_cmd_blockers_lists_active_categories(store, capsys):
    blockers.record(store, SLUG, "cat", "boom", 0)
    args = cli.build_parser().parse_args([SLUG, "blockers"])
    assert args.action == "list"
    assert args.category == ""
    args.handler(store, args)
    out = json.loads(capsys.readouterr().out)
    assert out == [{"category": "cat", "first_seen": 0, "last_seen": 0, "count": 1, "detail": "boom"}]


def test_cmd_blockers_clears_a_category(store, capsys):
    blockers.record(store, SLUG, "cat", "boom", 0)
    args = cli.build_parser().parse_args([SLUG, "blockers", "clear", "cat"])
    args.handler(store, args)
    assert json.loads(capsys.readouterr().out) == {"cleared": "cat"}
    assert blockers.active(store, SLUG, 0) == {}


def test_cmd_blockers_clear_without_a_category_refuses(store):
    args = cli.build_parser().parse_args([SLUG, "blockers", "clear"])
    with pytest.raises(cli.SwarmError, match="blockers clear needs a category"):
        args.handler(store, args)


def test_cmd_blockers_lists_categories_sorted_by_name(store, capsys):
    blockers.record(store, SLUG, "zeta", "z", 0)
    blockers.record(store, SLUG, "alpha", "a", 0)
    args = cli.build_parser().parse_args([SLUG, "blockers"])
    args.handler(store, args)
    out = json.loads(capsys.readouterr().out)
    assert [row["category"] for row in out] == ["alpha", "zeta"]


def test_blockers_action_choices_reject_unknown_values():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([SLUG, "blockers", "nonsense"])


def test_blockers_action_accepts_list_explicitly():
    args = cli.build_parser().parse_args([SLUG, "blockers", "list"])
    assert args.action == "list"


class _Ledger:
    def state(self, slug):
        return {"tasks": []}


class _Runtime:
    def quota_requirements(self, *args):
        return None
