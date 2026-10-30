import json

import fakeredis
import pytest

from scripts.hive import scope
from scripts.swarm import capacity, fleet_red
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_tick import FakeLedger

pytestmark = pytest.mark.unit
VALIDATED = {"validation": {"state": "validated"}}


class Listing:
    def __init__(self, redis):
        self._redis, self.listed = redis, []

    def __getattr__(self, name):
        return getattr(self._redis, name)

    def lrange(self, key, *args):
        self.listed.append(key)
        return self._redis.lrange(key, *args)


@pytest.fixture
def store():
    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def histories(store):
    history = store.key("sw", "history")
    for row in (
        {"name": "first", "task": "t1", "started_at": 1, "profile_decision": VALIDATED},
        {"name": "legacy", "task": "t1", "started_at": 2},
        {"name": "rejected", "task": "t1", "started_at": 3, "profile_decision": {"validation": {"state": "failed"}}},
        {"name": "odd", "task": "t1", "started_at": 4, "profile_decision": {"note": "validated"}},
        {"name": "recorded", "task": "t2", "started_at": 5, "profile_decision": VALIDATED},
        {"name": "last", "task": "t3", "started_at": 1793346579496, "profile_decision": VALIDATED},
    ):
        store.redis.rpush(history, json.dumps(row))
    store.record_launch("sw", AgentRecord("recorded", "eng", "t2", started_at=9), "failed", "boom")


def expected():
    started = {"state": "started", "error": ""}
    return [
        {"agent": "first", "task": "t1", "at": 1, **started},
        {"agent": "last", "task": "t3", "at": 1793346579496, **started},
        {
            "agent": "recorded",
            "task": "t2",
            "at": 9,
            "state": "failed",
            "error": "boom",
            "pane_id": "",
            "after": "",
            "head": "",
        },
    ]


def test_launches_filters_the_history_inside_redis(store):
    histories(store)
    listing = store.redis = Listing(store.redis)
    assert sorted(store.launches("sw"), key=lambda row: row["agent"]) == expected()
    assert store.key("sw", "history") not in listing.listed


def test_a_hive_member_reads_the_launch_history_itself(store, monkeypatch):
    histories(store)
    monkeypatch.setattr(scope, "member", lambda: "hive-1")
    listing = store.redis = Listing(store.redis)
    assert sorted(store.launches("sw"), key=lambda row: row["agent"]) == expected()
    assert store.key("sw", "history") in listing.listed


def test_ready_work_lists_pull_requests_once_for_every_lane(store, monkeypatch):
    store.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1, max_plan=1))
    pull = "https://github.com/acme/app/pull/{}"
    ledger = FakeLedger(
        [
            {"id": "e", "pr_url": pull.format(1)},
            {"id": "c", "lane": "ci", "pr_url": pull.format(2)},
        ]
    )
    listed = []
    monkeypatch.setattr(fleet_red, "read", lambda repo: listed.append(repo) or [])
    _, ready = capacity.ready_work("sw", store, ledger.state("sw"))
    assert {lane: [t["id"] for t in tasks] for lane, tasks in ready.items()} == {"eng": ["e"], "ci": ["c"], "plan": []}
    assert listed == ["acme/app"]
