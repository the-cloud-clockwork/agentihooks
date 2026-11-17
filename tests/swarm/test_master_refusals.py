"""The tick keeps each master placement refusal with its reason and time, past its pass and past the outage hand back."""

from typing import Any

import pytest

from scripts.hive import placement_waits
from scripts.inbox.store import InboxStore
from scripts.swarm import master_refusals, tick_master
from scripts.swarm.store import MASTER, RedisStore
from scripts.swarm.tick import MASTER_DOWN, SpawnError, tick
from scripts.tick.steps import masters as master_steps
from tests.swarm.test_tick import FakeRuntime, masters, store, tasks  # noqa: F401

MINUTE = 60_000
HOUR = 3_600_000
REFUSED = "the master lane runs on the workstation: no live hive takes master"
ROOM = "no hive for master has room: every hive is full"


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


@pytest.mark.feature("swarm-master-placement-refusals-kept")
def test_master_placement_refusals_stay_readable_after_the_pass_and_after_hand_back(store: RedisStore) -> None:  # noqa: F811
    runtime, ledger = RefusingRuntime(), tasks()
    for at in (MINUTE, 2 * MINUTE, 3 * MINUTE):
        assert REFUSED in tick("sw", store, ledger, runtime, at)
    kept = {"reason": REFUSED, "since": MINUTE, "at": 3 * MINUTE, "passes": 3}
    assert master_refusals.latest(store, "sw") == kept
    assert tick_master._launch_refusal(store, "sw") == f"master placement refused at 00:03 UTC: {REFUSED}"
    assert tick_master.read(store, "sw") == {"since": MINUTE}

    runtime.refused = ""
    actions = tick("sw", store, ledger, runtime, 10 * MINUTE)

    assert f"spawned master {runtime.masters[0][0]}" in actions
    assert [m.state for m in masters(store)] == ["working"]
    assert tick_master.read(store, "sw") == {}
    assert master_refusals.latest(store, "sw") == kept
    assert HOUR - MINUTE < store.redis.pttl(store.key("sw", master_refusals.KEY)) <= HOUR


@pytest.mark.feature("swarm-master-placement-refusals-kept")
def test_a_master_refused_room_on_every_hive_is_kept_with_its_reason(store: RedisStore) -> None:  # noqa: F811
    actions = master_steps._master("sw", store.config("sw"), store, FullRuntime(), 5 * MINUTE)

    assert actions == [f"the master waits: {ROOM}"]
    assert master_refusals.read(store, "sw") == [
        {"reason": f"the master waits: {ROOM}", "since": 5 * MINUTE, "at": 5 * MINUTE, "passes": 1}
    ]
    assert masters(store) == []


@pytest.mark.feature("swarm-master-placement-refusals-kept")
def test_the_master_down_notice_names_the_latest_refusal(store: RedisStore) -> None:  # noqa: F811
    inbox, ledger = InboxStore(store.redis), tasks()
    master_refusals.refused(store, "sw", REFUSED, 3 * MINUTE)
    item = inbox.send("operator", "master@sw", "anyone there")

    actions = master_steps._master_down("sw", store.config("sw"), store, ledger, FakeRuntime(), 4 * MINUTE)

    text = f"{MASTER_DOWN} The latest master placement refusal, at 00:03 UTC: {REFUSED}."
    assert actions == ["master down, told the operator about 1 waiting lines"]
    assert ledger.notes == [text]
    assert inbox.history(item.id)[-1]["reason"] == text


def test_the_master_down_notice_without_a_kept_refusal_is_unchanged(store: RedisStore) -> None:  # noqa: F811
    ledger = tasks()
    InboxStore(store.redis).send("operator", "master@sw", "anyone there")

    master_steps._master_down("sw", store.config("sw"), store, ledger, FakeRuntime(), 4 * MINUTE)

    assert ledger.notes == [MASTER_DOWN]


@pytest.mark.feature("swarm-master-placement-refusals-kept")
def test_swarm_status_names_the_latest_refusal_with_its_time(store: RedisStore) -> None:  # noqa: F811
    master_refusals.refused(store, "sw", "an older refusal", MINUTE)
    for at in (2 * MINUTE, 3 * MINUTE):
        master_refusals.refused(store, "sw", REFUSED, at)

    assert master_refusals.status_lines(store, "sw", 3 * MINUTE + 7 * MINUTE - 1) == [
        f"master placement refused  00:03 UTC  6 minutes ago  2 passes since 00:02 UTC  {REFUSED}"
    ]
    assert master_refusals.status_lines(store, "other", MINUTE) == []


def test_a_refusal_row_is_kept_for_the_whole_finding_window(store: RedisStore) -> None:  # noqa: F811
    master_refusals.refused(store, "sw", "first", MINUTE)
    master_refusals.refused(store, "sw", "second", MINUTE + HOUR - 1)

    assert [row["reason"] for row in master_refusals.read(store, "sw")] == ["first", "second"]
    assert HOUR - MINUTE < store.redis.pttl(store.key("sw", master_refusals.KEY)) <= HOUR

    master_refusals.refused(store, "sw", "third", MINUTE + HOUR)

    assert [row["reason"] for row in master_refusals.read(store, "sw")] == ["second", "third"]


def test_a_repeated_reason_extends_the_latest_row_only(store: RedisStore) -> None:  # noqa: F811
    for at, reason in ((MINUTE, "a"), (2 * MINUTE, "b"), (3 * MINUTE, "b")):
        assert master_refusals.refused(store, "sw", reason, at) == [reason]

    assert master_refusals.read(store, "sw") == [
        {"reason": "a", "since": MINUTE, "at": MINUTE, "passes": 1},
        {"reason": "b", "since": 2 * MINUTE, "at": 3 * MINUTE, "passes": 2},
    ]
    assert master_refusals.named(store, "sw") == "master placement refused at 00:03 UTC: b"
    assert master_refusals.named(store, "other") == ""
    assert master_refusals.latest(store, "other") == {}


def test_the_launch_refusal_without_a_kept_refusal_says_no_master_launched(store: RedisStore) -> None:  # noqa: F811
    assert tick_master._launch_refusal(store, "sw") == tick_master.NO_LAUNCH


def test_the_status_prune_keeps_the_master_wait_while_the_tick_still_sees_it_refused(store: RedisStore) -> None:  # noqa: F811
    placement_waits.observe(store, "sw", MASTER, MASTER, REFUSED, MINUTE)
    placement_waits.observe(store, "sw", "eng", "gone", REFUSED, MINUTE)

    placement_waits.prune(store, "sw", [], MINUTE + placement_waits.FRESH_MS - 1)

    assert set(placement_waits.read(store, "sw")) == {MASTER}

    placement_waits.prune(store, "sw", [], MINUTE + placement_waits.FRESH_MS)

    assert placement_waits.read(store, "sw") == {}
