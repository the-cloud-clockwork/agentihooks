import json
from types import SimpleNamespace

from scripts.doctor import detect, hive_pools
from scripts.hive import headroom, registry
from scripts.hive.auth import INDEX

MINUTE = 60_000
ROLL = 1_793_166_000_000
ORACLE = ("6ebe8950387c4e6a", "be7b18c10eb79574")
WORKER = "06db8fa06259f56a"
CPU_REFUSAL = f"no hive for eng has room: {WORKER} cpu: five minute load 15.80 on 16 CPUs, fits 0 more"


def _hive(hive_id, at, draining="no", beating=True):
    return {
        "id": hive_id,
        "ui": "no",
        "draining": draining,
        "heartbeat_at": at - 1_000 if beating else at - registry.LIVE_MS - 1,
    }


def _oracle_reading(at):
    return {"cpus": 8, "total_mb": 32000, "pod_total_mb": 16800, "measured_at": at}


def _worker_reading(at):
    return {"cpus": 16, "total_mb": 64000, "pod_total_mb": 19600, "measured_at": at}


def _refusals(at, start, count):
    return [{"id": f"rig-grade-swarm/t{n}", "seen_at": at} for n in range(start, start + count)]


def _roll(stays_ready):
    passes = []
    for minute, oracle, refused in (
        (-10, "ready", 0),
        (0, "draining", 6),
        (10, "draining", 14),
        (20, "gone", 20),
        (30, "ready", 0),
    ):
        at = ROLL + minute * MINUTE
        hives = [_hive(WORKER, at, "no")]
        readings = {WORKER: _worker_reading(at)}
        for index, hive_id in enumerate(ORACLE):
            state = "ready" if stays_ready and index == 1 else oracle
            hives.append(_hive(hive_id, at, "yes" if state == "draining" else "no", state != "gone"))
            if state != "gone":
                readings[hive_id] = _oracle_reading(at)
        passes.append((at, hives, readings, _refusals(at, minute * 10, refused)))
    return passes


def _replay(passes):
    state, raised = {}, []
    for at, hives, readings, refused in passes:
        state = hive_pools.observe(state, hives, readings, refused, at)
        raised.append(hive_pools.findings(state, at))
    return raised


def test_the_recorded_roll_raises_one_finding_for_the_oracle_pool():
    raised = _replay(_roll(stays_ready=False))
    assert {f.id for found in raised for f in found} == {"hive-pool-down/8cpu-16800mb"}
    assert [len(found) for found in raised] == [0, 0, 1, 1, 0]
    first, second = raised[2][0], raised[3][0]
    assert first.measure == 10
    assert first.summary == "no hive in pool 8cpu-16800mb is ready for 10 minutes, 20 tasks refused a launch meanwhile"
    assert first.threshold == "every hive in a pool not ready for ten minutes"
    assert first.evidence == (
        "pool 8cpu-16800mb",
        f"down since {ROLL}",
        "hive 6ebe8950387c4e6a",
        "hive be7b18c10eb79574",
    )
    assert second.measure == 20
    assert second.summary == "no hive in pool 8cpu-16800mb is ready for 20 minutes, 40 tasks refused a launch meanwhile"
    assert second.evidence == first.evidence


def test_a_roll_that_keeps_one_hive_ready_raises_nothing():
    assert _replay(_roll(stays_ready=True)) == [[], [], [], [], []]


def test_a_pool_recovers_and_its_down_clock_starts_again():
    passes = _roll(stays_ready=False)
    state = {}
    for at, hives, readings, refused in passes:
        state = hive_pools.observe(state, hives, readings, refused, at)
    assert state["down"] == {}
    at = ROLL + 40 * MINUTE
    hives = [_hive(WORKER, at), _hive(ORACLE[0], at, "yes"), _hive(ORACLE[1], at, "yes")]
    state = hive_pools.observe(state, hives, {h["id"]: _oracle_reading(at) for h in hives[1:]}, [], at)
    assert state["down"]["8cpu-16800mb"] == {"since": at, "hives": list(ORACLE), "refused": []}
    assert state["down"]["16cpu-19600mb"]["since"] == at


def test_refusals_seen_before_the_pool_went_down_are_not_counted():
    at = ROLL
    hives = [_hive(ORACLE[0], at, "yes")]
    old = [{"id": "sw/t1", "seen_at": at - 1}, {"id": "sw/t2", "seen_at": at}]
    state = hive_pools.observe({}, hives, {ORACLE[0]: _oracle_reading(at)}, old, at)
    assert state["down"]["8cpu-16800mb"]["refused"] == ["sw/t2"]


def test_a_hive_without_a_reading_or_a_known_pool_and_the_interface_hive_join_no_pool():
    at = ROLL
    hives = [_hive("new", at), {**_hive("ws", at), "ui": "yes"}]
    state = hive_pools.observe({}, hives, {"ws": _oracle_reading(at)}, [], at)
    assert state == {"members": {}, "down": {}}


def test_skipped_hives_listed_first_leave_the_later_pool_counted():
    at = ROLL
    hives = [{**_hive("ws", at), "ui": "yes"}, _hive("new", at), _hive(ORACLE[0], at, "yes")]
    readings = {"ws": _oracle_reading(at), ORACLE[0]: _oracle_reading(at)}
    state = hive_pools.observe({}, hives, readings, [], at)
    assert state["members"] == {ORACLE[0]: "8cpu-16800mb"}
    assert list(state["down"]) == ["8cpu-16800mb"]


def test_a_pool_down_under_the_threshold_does_not_hide_a_later_pool_past_it():
    at = ROLL
    entry = {"hives": [], "refused": []}
    state = {
        "down": {
            "16cpu-19600mb": {**entry, "since": at - MINUTE},
            "8cpu-16800mb": {**entry, "since": at - 10 * MINUTE},
        }
    }
    assert [f.id for f in hive_pools.findings(state, at)] == ["hive-pool-down/8cpu-16800mb"]


def test_a_pool_whose_hives_lost_their_records_is_forgotten():
    at = ROLL
    state = hive_pools.observe({}, [_hive(ORACLE[0], at, "yes")], {ORACLE[0]: _oracle_reading(at)}, [], at)
    assert hive_pools.observe(state, [], {}, [], at + 20 * MINUTE) == {"members": {}, "down": {}}


def test_a_pool_without_a_pod_limit_is_named_by_its_host_memory():
    assert hive_pools.pool({"cpus": 4, "total_mb": 7900}) == "4cpu-7900mb"


def test_the_hive_pool_reader_keeps_its_state_and_counts_room_refusals_in_every_swarm():
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    store = SimpleNamespace(redis=redis, key=lambda slug, name: f"t:{slug}:{name}", slugs=lambda: ["sw", "other"])
    at = ROLL
    for hive_id in ORACLE:
        redis.sadd(INDEX, hive_id)
        redis.hset(registry.key(hive_id), mapping={"heartbeat_at": at - 1_000, "draining": "yes"})
        redis.set(headroom.key(hive_id), json.dumps(_oracle_reading(at)))
    redis.hset("t:sw:hive-placement-waits", "t1", json.dumps({"reason": CPU_REFUSAL, "seen_at": at}))
    redis.hset("t:other:hive-placement-waits", "t2", json.dumps({"reason": CPU_REFUSAL, "seen_at": at}))
    redis.hset("t:other:hive-placement-waits", "t3", json.dumps({"reason": "no UI hive live", "seen_at": at}))
    assert detect.readers(store, None, "sw", at, environ={})["hive pool"]() == []
    [found] = detect.readers(store, None, "sw", at + 10 * MINUTE, environ={})["hive pool"]()
    assert found.id == "hive-pool-down/8cpu-16800mb"
    assert found.summary.endswith("2 tasks refused a launch meanwhile")
    assert json.loads(redis.get("t:sw:doctor-hive-pools"))["down"]["8cpu-16800mb"]["refused"] == ["other/t2", "sw/t1"]


def test_a_measured_hive_is_ready_only_while_it_beats_and_takes_launches():
    reading = _oracle_reading(ROLL)
    assert hive_pools.ready(_hive(ORACLE[0], ROLL), reading, ROLL)
    assert not hive_pools.ready(_hive(ORACLE[0], ROLL, beating=False), reading, ROLL)
    assert not hive_pools.ready(_hive(ORACLE[0], ROLL, "yes"), reading, ROLL)
    assert not hive_pools.ready(_hive(ORACLE[0], ROLL), None, ROLL)
