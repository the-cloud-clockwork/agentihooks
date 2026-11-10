"""Hive admission outcomes are counted per swarm and exported as admission samples."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.hive import admission, launch, placement_waits
from scripts.swarm import controller, metrics_service
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import ACK_LATER
from scripts.swarm_v2.runtime.base import Status
from tests.hive.test_controller_launch import request_for, setup, write_hive  # noqa: F401

NOW = 1_800_000_000_000
CPU_REFUSAL = f"no hive has room: h1 {admission.CPU}: load 3.9 of 4 cpus"


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    return store


def _totals(store: RedisStore) -> dict[str, str]:
    return store.redis.hgetall(store.key("sw", placement_waits.TOTALS))


def test_a_new_refusal_counts_its_category_once(store: RedisStore) -> None:
    placement_waits.observe(store, "sw", "eng", "t1", CPU_REFUSAL, NOW)
    placement_waits.observe(store, "sw", "eng", "t1", CPU_REFUSAL, NOW + 1)
    assert _totals(store) == {admission.CPU: "1"}


def test_a_changed_refusal_counts_the_new_category(store: RedisStore) -> None:
    placement_waits.observe(store, "sw", "eng", "t1", CPU_REFUSAL, NOW)
    placement_waits.observe(store, "sw", "eng", "t1", "no live hive takes ui", NOW + 1)
    assert _totals(store) == {admission.CPU: "1", admission.PLACEMENT: "1"}


def test_a_lane_change_counts_the_same_refusal_again(store: RedisStore) -> None:
    placement_waits.observe(store, "sw", "eng", "t1", CPU_REFUSAL, NOW)
    placement_waits.observe(store, "sw", "ci", "t1", CPU_REFUSAL, NOW + 1)
    assert _totals(store) == {admission.CPU: "2"}


def test_a_selected_placement_clears_the_wait_and_counts_nothing(store: RedisStore) -> None:
    placement_waits.observe(store, "sw", "eng", "t1", CPU_REFUSAL, NOW)
    placement_waits.observe(store, "sw", "eng", "t1", "", NOW + 1)
    placement_waits.observe(store, "sw", "eng", "t2", "", NOW + 2)
    assert _totals(store) == {admission.CPU: "1"}
    assert placement_waits.read(store, "sw") == {}


def test_each_admitted_launch_counts_once(store: RedisStore) -> None:
    placement_waits.admitted(store, "sw")
    placement_waits.admitted(store, "sw")
    assert _totals(store) == {placement_waits.ADMITTED: "2"}


def test_admission_rows_sum_hive_outcomes_with_pending_totals(store: RedisStore) -> None:
    store.redis.hset(store.key("sw", "pending-admission-total"), mapping={"admitted": "5", "deferred": "2"})
    placement_waits.observe(store, "sw", "eng", "t1", CPU_REFUSAL, NOW)
    placement_waits.admitted(store, "sw")
    rows = metrics_service.admission_rows(store, "sw", NOW)
    assert [(row["outcome"], row["total"]) for row in rows] == [("admitted", 6), ("cpu", 1), ("deferred", 2)]
    assert rows[1]["event_id"] == f"admission:sw:{NOW}:cpu"
    for row in rows:
        metrics_service.ADMISSION.check(row)


def test_a_hive_refusal_alone_yields_admission_rows(store: RedisStore) -> None:
    placement_waits.observe(store, "sw", "eng", "t1", CPU_REFUSAL, NOW)
    rows = metrics_service.admission_rows(store, "sw", NOW)
    assert [(row["outcome"], row["total"]) for row in rows] == [(admission.CPU, 1)]


def test_a_hive_launch_counts_admitted_for_its_swarm(setup: SimpleNamespace) -> None:  # noqa: F811
    assert isinstance(setup.launcher, launch.HiveLaunch)
    write_hive(setup)
    request = request_for(setup)
    request.task[ACK_LATER] = True
    result = setup.launcher.spawn(request)
    assert result.status is Status.PENDING
    assert setup.store.redis.hgetall(setup.store.key("fixture", placement_waits.TOTALS)) == {
        placement_waits.ADMITTED: "1"
    }


@pytest.mark.parametrize(("runtime", "counted"), [(Mock(), False), (Mock(spec=["spawn"]), True)])
def test_a_local_launch_counts_admitted_only_without_a_hive_runtime(
    setup: SimpleNamespace,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    runtime: Mock,
    counted: bool,
) -> None:
    from scripts.swarm import commands

    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    fenced = controller.FencedRuntime(setup.store, setup.config.slug, setup.held, runtime, controller.LOCAL)
    monkeypatch.setattr(fenced, "_refusal", lambda lane, task, name: "")
    fenced.spawn(setup.config, "eng", "eng name", {"id": "task", "seat": "eng-1@fixture"})
    totals = setup.store.redis.hgetall(setup.store.key(setup.config.slug, placement_waits.TOTALS))
    assert (placement_waits.ADMITTED in totals) is counted
