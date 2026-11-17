"""The tick keeps each master placement refusal per seat with its reason and time, past its pass and past the outage
hand back."""

from typing import Any

import pytest

from scripts.hive import placement_waits
from scripts.inbox.store import InboxStore
from scripts.swarm import cli, master_refusals, tick_master
from scripts.swarm.store import MASTER, RedisStore
from scripts.swarm.tick import MASTER_DOWN, SpawnError, tick
from scripts.swarm_v2 import master_fill
from scripts.swarm_v2.masters import MasterSeats
from scripts.tick.steps import masters as master_steps
from tests.hive.test_controller_launch import setup  # noqa: F401
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_tick import FakeRuntime, masters, store, tasks  # noqa: F401

MINUTE = 60_000
DAY = 24 * 60 * MINUTE
SEAT_TWO = "master-2"
REFUSED = "the master lane runs on the workstation: no live hive takes master"
ROOM = "no hive for master has room: every hive is full"
SLOT = "no session slot for the master, waiting"


class RefusingRuntime(FakeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.refused = REFUSED

    def placement_refusal(self, lane: str, task: dict[str, Any]) -> str:
        return self.refused if lane == MASTER else ""


class FullRuntime(FakeRuntime):
    def spawn(self, config: Any, lane: str, name: str, task: dict[str, Any]) -> Any:
        if lane == MASTER:
            raise SpawnError(ROOM, "unavailable")
        return super().spawn(config, lane, name, task)


def row(reason: str, since: int, at: int, passes: int = 1) -> dict[str, Any]:
    return {"reason": reason, "since": since, "at": at, "passes": passes}


@pytest.mark.feature("swarm-master-placement-refusals-kept")
def test_master_placement_refusals_stay_readable_after_the_pass_and_after_hand_back(store: RedisStore) -> None:  # noqa: F811
    runtime, ledger = RefusingRuntime(), tasks()
    for at in (MINUTE, 2 * MINUTE, 3 * MINUTE):
        assert REFUSED in tick("sw", store, ledger, runtime, at)
    kept = row(REFUSED, MINUTE, 3 * MINUTE, 3)
    assert master_refusals.latest(store, "sw", MASTER) == kept
    assert tick_master._launch_refusal(store, "sw", 5 * MINUTE) == (
        f"master placement last refused 2 minutes ago, at 00:03 UTC: {REFUSED}"
    )
    assert tick_master.read(store, "sw") == {"since": MINUTE}

    runtime.refused = ""
    actions = tick("sw", store, ledger, runtime, 10 * MINUTE)

    assert f"spawned master {runtime.masters[0][0]}" in actions
    assert [m.state for m in masters(store)] == ["working"]
    assert tick_master.read(store, "sw") == {}
    assert master_refusals.latest(store, "sw", MASTER) == kept
    assert DAY - MINUTE < store.redis.pttl(store.key("sw", master_refusals.KEY)) <= DAY


@pytest.mark.feature("swarm-master-placement-refusals-kept")
def test_a_fenced_master_refusal_is_kept_through_the_master_step_status_findings_and_prune(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.swarm import controller, lease, status
    from scripts.swarm.profile_choice import ProfileDecision
    from scripts.swarm_v2.runtime.base import RuntimeRouter
    from scripts.swarm_v2.runtime.routed import RoutedRuntime

    setup.herdr._profile_decision.return_value = ProfileDecision("master", "lane", "master")
    routed = RoutedRuntime(setup.herdr, RuntimeRouter([]))
    fenced = controller.FencedRuntime(setup.store, setup.config.slug, setup.held, routed, "compose")
    slug, at = setup.config.slug, lease.now_ms(setup.store)

    [refused] = master_steps._master(slug, setup.config, setup.store, fenced, at)

    wait = placement_waits.read(setup.store, slug)[MASTER]
    assert refused == f"{controller.WORKSTATION.format(lane=MASTER)}: {wait['reason']}"
    monkeypatch.setattr(status.activity, "counts", lambda slug: {})
    status.findings(setup.store, slug, setup.config, [], [])
    assert placement_waits.read(setup.store, slug)[MASTER] == wait
    assert master_refusals.latest(setup.store, slug, MASTER) == row(refused, at, at)


@pytest.mark.feature("swarm-master-placement-refusals-kept")
def test_a_master_refused_room_on_every_hive_is_kept_with_its_reason(store: RedisStore) -> None:  # noqa: F811
    actions = master_steps._master("sw", store.config("sw"), store, FullRuntime(), 5 * MINUTE)

    assert actions == [f"the master waits: {ROOM}"]
    assert master_refusals.read(store, "sw", MASTER) == [row(f"the master waits: {ROOM}", 5 * MINUTE, 5 * MINUTE)]
    assert masters(store) == []


@pytest.mark.feature("swarm-master-placement-refusals-kept")
def test_a_master_with_no_session_slot_is_kept_with_its_reason_and_its_wait_time(store: RedisStore) -> None:  # noqa: F811
    from scripts.swarm.tick import MASTER_WAITING

    actions = master_steps._master("sw", store.config("sw"), store, FakeRuntime(full=True), 5 * MINUTE)

    assert actions == [SLOT]
    assert master_refusals.read(store, "sw", MASTER) == [row(SLOT, 5 * MINUTE, 5 * MINUTE)]
    assert store.redis.hget(MASTER_WAITING, "sw") == str(5 * MINUTE)


@pytest.mark.feature("swarm-master-placement-refusals-kept")
def test_every_master_seat_keeps_its_own_refusals(store: RedisStore) -> None:  # noqa: F811
    MasterSeats(store.redis).set_count("sw", 2)

    assert master_fill.run("sw", store.config("sw"), store, RefusingRuntime(), {}, MINUTE) == [REFUSED]
    assert master_fill.run("sw", store.config("sw"), store, FakeRuntime(full=True), {}, 2 * MINUTE) == [SLOT]

    assert master_refusals.read(store, "sw", SEAT_TWO) == [
        row(REFUSED, MINUTE, MINUTE),
        row(SLOT, 2 * MINUTE, 2 * MINUTE),
    ]
    assert master_refusals.read(store, "sw", MASTER) == []


def test_master_fill_reports_every_refused_seat(store: RedisStore) -> None:  # noqa: F811
    MasterSeats(store.redis).set_count("sw", 3)

    assert master_fill.run("sw", store.config("sw"), store, RefusingRuntime(), {}, MINUTE) == [REFUSED, REFUSED]
    assert [master_refusals.read(store, "sw", seat) for seat in (SEAT_TWO, "master-3")] == [
        [row(REFUSED, MINUTE, MINUTE)],
        [row(REFUSED, MINUTE, MINUTE)],
    ]


def test_a_forced_launch_that_starts_nothing_names_the_kept_refusal_with_its_age(store: RedisStore) -> None:  # noqa: F811
    master_refusals.record(store, "sw", MASTER, REFUSED, 4 * MINUTE)
    tick_master._save(store, "sw", {"since": 1})

    actions = tick_master.run("sw", store, tasks(), FakeRuntime(), 5 * MINUTE + 1, lambda: [], {})

    failure = f"master placement last refused 1 minutes ago, at 00:04 UTC: {REFUSED}"
    assert actions == ["master down 5 minutes, forced a master launch", failure, "no live engineer to promote"]
    assert tick_master.read(store, "sw")["failure"] == failure


@pytest.mark.feature("swarm-master-placement-refusals-kept")
def test_the_master_down_notice_names_the_latest_refusal_with_its_age(store: RedisStore) -> None:  # noqa: F811
    inbox, ledger = InboxStore(store.redis), tasks()
    master_refusals.record(store, "sw", MASTER, REFUSED, 3 * MINUTE)
    master_refusals.record(store, "sw", SEAT_TWO, "a seat two refusal", 4 * MINUTE)
    item = inbox.send("operator", "master@sw", "anyone there")

    actions = master_steps._master_down("sw", store.config("sw"), store, ledger, FakeRuntime(), 5 * MINUTE)

    text = f"{MASTER_DOWN} The master placement was last refused 2 minutes ago, at 00:03 UTC: {REFUSED}."
    assert actions == ["master down, told the operator about 1 waiting lines"]
    assert ledger.notes == [text]
    assert inbox.history(item.id)[-1]["reason"] == text


def test_the_master_down_notice_without_a_kept_refusal_is_unchanged(store: RedisStore) -> None:  # noqa: F811
    ledger = tasks()
    InboxStore(store.redis).send("operator", "master@sw", "anyone there")

    master_steps._master_down("sw", store.config("sw"), store, ledger, FakeRuntime(), 4 * MINUTE)

    assert ledger.notes == [MASTER_DOWN]


@pytest.mark.feature("swarm-master-placement-refusals-kept")
def test_swarm_status_names_each_seat_latest_refusal_with_its_time(store: RedisStore) -> None:  # noqa: F811
    master_refusals.record(store, "sw", SEAT_TWO, "a seat two refusal", 2 * MINUTE)
    master_refusals.record(store, "sw", MASTER, "an older refusal", MINUTE)
    for at in (2 * MINUTE, 3 * MINUTE):
        master_refusals.record(store, "sw", MASTER, REFUSED, at)

    assert master_refusals.status_lines(store, "sw", 10 * MINUTE - 1) == [
        f"master placement refused  master@sw  00:03 UTC  6 minutes ago  2 passes since 00:02 UTC  {REFUSED}",
        "master placement refused  master-2@sw  00:02 UTC  7 minutes ago  1 passes since 00:02 UTC  a seat two refusal",
    ]
    assert master_refusals.status_lines(store, "other", MINUTE) == []


def test_swarm_status_reads_every_seat_across_hash_pages(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    for index in range(2, 14):
        master_refusals.record(store, "sw", f"master-{index}", "refused", MINUTE)
    monkeypatch.setattr(master_refusals, "ROWS", 5)

    lines = master_refusals.status_lines(store, "sw", MINUTE)

    assert sorted(line.split("  ")[1] for line in lines) == sorted(f"master-{index}@sw" for index in range(2, 14))


def test_the_seat_pages_ask_for_the_row_cap(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    master_refusals.record(store, "sw", MASTER, REFUSED, MINUTE)
    scan, counts = store.redis.hscan, []

    def counted(key: str, cursor: int = 0, match: Any = None, count: Any = None) -> Any:
        counts.append(count)
        return scan(key, cursor, match=match, count=count)

    monkeypatch.setattr(store.redis, "hscan", counted)

    assert list(master_refusals.seats(store, "sw")) == [MASTER]
    assert counts == [master_refusals.ROWS]


@pytest.mark.feature("swarm-master-placement-refusals-kept")
def test_status_text_names_the_latest_master_placement_refusal(  # noqa: F811
    env: Any,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, _, _ = env
    run("sw", "create", "--repo", "/repo")
    master_refusals.record(swarm, "sw", MASTER, "no live hive takes master", 3 * MINUTE)
    monkeypatch.setattr(cli, "now_ms", lambda: 10 * MINUTE)

    run("sw", "status")

    line = "master placement refused  master@sw  00:03 UTC  7 minutes ago  1 passes since 00:03 UTC  no live hive takes master"
    assert line in capsys.readouterr().out.splitlines()


def test_a_refusal_row_is_kept_for_a_whole_day(store: RedisStore) -> None:  # noqa: F811
    master_refusals.record(store, "sw", MASTER, "first", MINUTE)
    master_refusals.record(store, "sw", MASTER, "second", MINUTE + DAY - 1)

    assert [kept["reason"] for kept in master_refusals.read(store, "sw", MASTER)] == ["first", "second"]
    assert DAY - MINUTE < store.redis.pttl(store.key("sw", master_refusals.KEY)) <= DAY

    master_refusals.record(store, "sw", MASTER, "third", MINUTE + DAY)

    assert [kept["reason"] for kept in master_refusals.read(store, "sw", MASTER)] == ["second", "third"]


def test_a_seat_keeps_its_newest_hundred_rows(store: RedisStore) -> None:  # noqa: F811
    for index in range(101):
        master_refusals.record(store, "sw", MASTER, f"reason {index}", MINUTE + index)

    kept = [kept["reason"] for kept in master_refusals.read(store, "sw", MASTER)]
    assert kept == [f"reason {index}" for index in range(1, 101)]


def test_a_seat_refused_a_day_ago_is_no_longer_named_while_another_seat_keeps_the_record(store: RedisStore) -> None:  # noqa: F811
    master_refusals.record(store, "sw", MASTER, "a day old refusal", MINUTE)
    master_refusals.record(store, "sw", SEAT_TWO, "a seat two refusal", MINUTE + DAY - 1)
    day_old = "master placement last refused 1439 minutes ago, at 00:01 UTC: a day old refusal"

    assert master_refusals.named(store, "sw", MINUTE + DAY - 1) == day_old
    assert len(master_refusals.status_lines(store, "sw", MINUTE + DAY - 1)) == 2

    assert master_refusals.named(store, "sw", MINUTE + DAY) == ""
    assert master_refusals.notice(store, "sw", MASTER_DOWN, MINUTE + DAY) == MASTER_DOWN
    assert master_refusals.status_lines(store, "sw", MINUTE + DAY) == [
        "master placement refused  master-2@sw  00:00 UTC  0 minutes ago  1 passes since 00:00 UTC  a seat two refusal"
    ]


def test_a_repeated_reason_extends_the_latest_row_only(store: RedisStore) -> None:  # noqa: F811
    for at, reason in ((MINUTE, "a"), (2 * MINUTE, "b"), (3 * MINUTE, "b")):
        assert master_refusals.record(store, "sw", MASTER, reason, at) == [reason]

    assert master_refusals.read(store, "sw", MASTER) == [row("a", MINUTE, MINUTE), row("b", 2 * MINUTE, 3 * MINUTE, 2)]
    assert (
        master_refusals.named(store, "sw", 4 * MINUTE) == "master placement last refused 1 minutes ago, at 00:03 UTC: b"
    )
    assert master_refusals.named(store, "other", 4 * MINUTE) == ""
    assert master_refusals.latest(store, "other", MASTER) == {}


def test_the_launch_refusal_without_a_kept_refusal_says_no_master_launched(store: RedisStore) -> None:  # noqa: F811
    master_refusals.record(store, "sw", SEAT_TWO, "a seat two refusal", MINUTE)

    assert tick_master._launch_refusal(store, "sw", 2 * MINUTE) == tick_master.NO_LAUNCH


def test_the_status_prune_keeps_every_master_seat_wait_while_the_tick_still_sees_it(store: RedisStore) -> None:  # noqa: F811
    placement_waits.observe(store, "sw", MASTER, MASTER, REFUSED, MINUTE)
    placement_waits.observe(store, "sw", MASTER, SEAT_TWO, REFUSED, MINUTE)
    placement_waits.observe(store, "sw", "eng", "gone", REFUSED, MINUTE)

    placement_waits.prune(store, "sw", [], MINUTE + placement_waits.FRESH_MS - 1)

    assert set(placement_waits.read(store, "sw")) == {MASTER, SEAT_TWO}

    placement_waits.prune(store, "sw", [], MINUTE + placement_waits.FRESH_MS)

    assert placement_waits.read(store, "sw") == {}
