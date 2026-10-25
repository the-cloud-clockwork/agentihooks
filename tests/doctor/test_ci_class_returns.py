import json
from dataclasses import replace

import pytest

from scripts.doctor import ci_speed, loop
from scripts.swarm.health.findings import MINUTE_MS
from scripts.swarm.health.verdicts import VerdictStore
from scripts.swarm.store import RedisStore

pytestmark = pytest.mark.unit
NOW = 1000 * MINUTE_MS


def grouped(kind, values, offset=0):
    members = {
        str(index): {
            "order": [index + offset, 1],
            "run": str(index + offset),
            "measure": value,
            "evidence": f"run {index + offset}, {value} seconds",
            "during": [],
        }
        for index, value in enumerate(values)
    }
    [finding] = ci_speed.grouped(
        {"group": {"kind": kind, "subject": "merge_group", "text": "queue", "members": members}}
    )
    return finding


@pytest.mark.parametrize("kind", ["ci runner wait", "ci tier wall time"])
@pytest.mark.parametrize(
    ("values", "returns"),
    [
        ([900, 1000], False),
        ([900, 1054], False),
        ([900, 1200], False),
        ([900, 1201], True),
        ([901, 1000], True),
        ([800, 1000], False),
    ],
)
def test_established_ci_classes_return_only_for_numeric_growth(kind, values, returns):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    first = grouped(kind, [900, 900, 1000])
    loop.record(store, "doctor", [first], NOW, MINUTE_MS)
    judgments = loop.verdicts(store, "doctor")
    judgments.judge(first.id, "established", "known", "master", NOW)
    changed = grouped(kind, [values[0], values[0], values[1]], offset=10)
    assert bool(loop.record(store, "doctor", [changed], NOW + MINUTE_MS, MINUTE_MS)) is returns
    assert loop.record(store, "doctor", [changed], NOW + 2 * MINUTE_MS, MINUTE_MS) == []


@pytest.mark.parametrize("kind", ["ci runner wait", "ci tier wall time"])
def test_existing_ci_verdicts_use_their_recorded_numeric_baseline(kind):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    first = grouped(kind, [900, 900, 1000])
    loop.record(store, "doctor", [first], NOW, MINUTE_MS)
    judgments = loop.verdicts(store, "doctor")
    judgments.judge(first.id, "established", "known", "master", NOW)
    record = json.loads(store.redis.hget(judgments.key, first.id))
    record.pop("metrics", None)
    record["verdict"].pop("metrics", None)
    store.redis.hset(judgments.key, first.id, json.dumps(record))
    assert loop.record(store, "doctor", [grouped(kind, [800, 800, 1054], 10)], NOW + MINUTE_MS, MINUTE_MS) == []
    assert loop.record(store, "doctor", [grouped(kind, [901, 901, 1000], 20)], NOW + 2 * MINUTE_MS, MINUTE_MS)


@pytest.mark.parametrize("kind", ["ci runner wait", "ci tier wall time"])
def test_numeric_ci_baselines_survive_display_changes(kind):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    first = replace(grouped(kind, [900, 900, 1000]), evidence=("CI class timing",))
    judgments = VerdictStore(store.redis, loop.verdicts(store, "doctor").key)
    judgments.visible([first], NOW, MINUTE_MS)
    verdict = judgments.judge(first.id, "established", "known", "master", NOW)
    assert verdict["metrics"] == [1000, 900]
    changed = replace(grouped(kind, [901, 901, 1000], 10), evidence=("New display",))
    assert loop.record(store, "doctor", [changed], NOW + MINUTE_MS, MINUTE_MS)


@pytest.mark.parametrize("kind", ["ci runner wait", "ci tier wall time"])
def test_non_established_ci_classes_keep_existing_return_behavior(kind):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    first = grouped(kind, [900, 900, 1000])
    loop.record(store, "doctor", [first], NOW, MINUTE_MS)
    loop.verdicts(store, "doctor").judge(first.id, "early-real", "watch", "master", NOW)
    assert loop.record(store, "doctor", [grouped(kind, [800, 800, 1054], 10)], NOW + MINUTE_MS, MINUTE_MS)


@pytest.mark.parametrize("kind", ["ci runner wait", "ci tier wall time"])
def test_legacy_ci_verdict_without_a_median_can_return_for_worst_growth(kind):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    first = replace(grouped(kind, [900, 900, 1000]), metrics=(), evidence=("Legacy timing",))
    loop.record(store, "doctor", [first], NOW, MINUTE_MS)
    loop.verdicts(store, "doctor").judge(first.id, "established", "known", "master", NOW)
    assert loop.record(store, "doctor", [grouped(kind, [800, 800, 1054], 10)], NOW + MINUTE_MS, MINUTE_MS) == []
    assert loop.record(store, "doctor", [grouped(kind, [800, 800, 1201], 20)], NOW + 2 * MINUTE_MS, MINUTE_MS)
