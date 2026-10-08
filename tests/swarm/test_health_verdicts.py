import json

import pytest

from scripts.swarm.health import verdicts
from scripts.swarm.health.findings import Finding
from scripts.swarm.keyspace import ROOT as KEY_ROOT
from scripts.swarm.store import SwarmError

pytestmark = pytest.mark.xdist_group("fakeredis")

KEY = f"{KEY_ROOT}:swarm:sw:findings"
HOUR = 60 * 60_000
T0 = 10_000_000_000


class Writes:
    def __init__(self):
        import fakeredis

        self.redis = fakeredis.FakeRedis(decode_responses=True)
        self.log = []

    def __getattr__(self, name):
        if name in ("hset", "hdel", "delete", "set"):
            self.log.append(name)
        return getattr(self.redis, name)


def watching(calls):
    return Finding(
        "over monitoring",
        "sw-master-1",
        "more watch calls than actions",
        (f"{calls} watch calls", "2 actions"),
        "at least 20 watch calls and more than 5 per action",
        calls,
    )


@pytest.fixture
def board():
    client = Writes()
    return client, verdicts.VerdictStore(client, KEY)


def shown(board, found, now):
    return [f["id"] for f in board.visible(found, now, HOUR)]


def test_a_finding_id_comes_from_its_kind_and_subject():
    assert watching(33).id == "over-monitoring/sw-master-1"
    assert watching(40).id == watching(33).id


def test_a_verdicted_finding_stays_hidden_through_the_cooldown(board):
    _, store = board
    assert shown(store, [watching(33)], T0) == ["over-monitoring/sw-master-1"]
    store.judge("over-monitoring/sw-master-1", "false-positive", "ledger re-arms", "sw-master-1", T0)
    for later in (T0 + 1, T0 + HOUR // 2, T0 + HOUR - 1):
        assert shown(store, [watching(90)], later) == []


def test_after_the_cooldown_it_returns_only_if_its_evidence_grew(board):
    _, store = board
    store.visible([watching(33)], T0, HOUR)
    store.judge("over-monitoring/sw-master-1", "early-real", "", "operator", T0)
    assert shown(store, [watching(33)], T0 + HOUR) == []
    assert shown(store, [watching(34)], T0 + HOUR + 1) == ["over-monitoring/sw-master-1"]


def aging(minutes, wakes):
    return Finding("inbox past window", "i1", f"pending for {minutes} minutes", (f"{wakes} wakes",), "window", minutes)


def test_with_new_evidence_required_a_measure_grown_by_age_alone_stays_judged(board):
    _, store = board
    store.visible([aging(10, 0)], T0, HOUR, new_evidence=True)
    store.judge("inbox-past-window/i1", "false-positive", "", "operator", T0)
    assert store.visible([aging(200, 0)], T0 + 3 * HOUR, HOUR, new_evidence=True) == []
    assert shown(store, [aging(200, 0)], T0 + 3 * HOUR) == ["inbox-past-window/i1"]


def test_with_new_evidence_required_changed_evidence_and_a_grown_measure_bring_it_back(board):
    _, store = board
    store.visible([aging(10, 0)], T0, HOUR, new_evidence=True)
    store.judge("inbox-past-window/i1", "false-positive", "", "operator", T0)
    assert store.visible([aging(9, 1)], T0 + HOUR, HOUR, new_evidence=True) == []
    [back] = store.visible([aging(70, 1)], T0 + HOUR, HOUR, new_evidence=True)
    assert back["id"] == "inbox-past-window/i1"


def test_a_verdict_kept_without_its_evidence_compares_with_the_last_pass(board):
    client, store = board
    store.visible([aging(10, 0)], T0, HOUR, new_evidence=True)
    store.judge("inbox-past-window/i1", "false-positive", "", "operator", T0)
    record = json.loads(client.redis.hget(KEY, "inbox-past-window/i1"))
    del record["verdict"]["evidence"]
    client.redis.hset(KEY, "inbox-past-window/i1", json.dumps(record))
    assert store.visible([aging(100, 0)], T0 + 2 * HOUR, HOUR, new_evidence=True) == []
    [back] = store.visible([aging(110, 1)], T0 + 2 * HOUR, HOUR, new_evidence=True)
    assert back["id"] == "inbox-past-window/i1"


def test_it_returns_at_most_once_and_carries_its_earlier_verdict(board):
    _, store = board
    store.visible([watching(33)], T0, HOUR)
    store.judge("over-monitoring/sw-master-1", "established", "real", "operator", T0)
    [back] = store.visible([watching(40)], T0 + HOUR, HOUR)
    assert back["verdict"] == {"value": "established", "note": "real", "by": "operator", "at": T0}
    assert shown(store, [watching(40)], T0 + 2 * HOUR) == ["over-monitoring/sw-master-1"]
    store.judge("over-monitoring/sw-master-1", "resolved", "", "operator", T0 + 2 * HOUR)
    assert shown(store, [watching(41)], T0 + 2 * HOUR + 1) == []


def test_an_identical_finding_writes_nothing(board):
    client, store = board
    store.visible([watching(33)], T0, HOUR)
    assert client.log == ["hset"]
    store.visible([watching(33)], T0 + 5_000, HOUR)
    store.visible([watching(33)], T0 + 9_000, HOUR)
    assert client.log == ["hset"]
    store.visible([watching(34)], T0 + 10_000, HOUR)
    assert client.log == ["hset", "hset"]


def test_a_verdict_is_kept_with_the_swarm_keys_and_names_who_gave_it(board):
    client, store = board
    store.visible([watching(33)], T0, HOUR)
    store.judge("over-monitoring/sw-master-1", "insufficient-evidence", "one day only", "sw-master-1", T0)
    record = json.loads(client.redis.hget(KEY, "over-monitoring/sw-master-1"))
    assert record["verdict"] == {
        "value": "insufficient-evidence",
        "note": "one day only",
        "by": "sw-master-1",
        "at": T0,
        "measure": 33,
        "evidence": ["33 watch calls", "2 actions"],
    }


def test_a_verdict_must_be_one_of_the_five_and_name_a_known_finding(board):
    _, store = board
    store.visible([watching(33)], T0, HOUR)
    with pytest.raises(SwarmError, match="false-positive, early-real, established, insufficient-evidence, resolved"):
        store.judge("over-monitoring/sw-master-1", "wrong", "", "operator", T0)
    with pytest.raises(SwarmError, match="no finding idle-with-claim/sw-eng-9"):
        store.judge("idle-with-claim/sw-eng-9", "resolved", "", "operator", T0)


def test_cooldown_minutes_come_from_the_environment():
    from scripts.swarm.health import findings as health

    assert health.limits({}).cooldown_minutes == 60
    assert health.limits({"AGENTIHOOKS_HEALTH_COOLDOWN_MINUTES": "5"}).cooldown_minutes == 5


def test_the_master_prompt_gives_every_new_finding_a_verdict():
    from scripts.swarm import prompt
    from scripts.swarm.store import MASTER

    text = prompt.build("sw", "/repo", MASTER, "sw-master-1", {"id": MASTER})
    duty = next(line for line in text.splitlines() if "verdict" in line)
    assert "every new health finding" in duty
    assert "agentihooks swarm sw --as sw-master-1 verdict <finding id>" in duty
    for value in verdicts.VERDICTS:
        assert value in duty


def test_a_shown_finding_carries_when_it_was_first_seen(board):
    _, store = board
    assert store.visible([watching(33)], T0, HOUR)[0]["seen_at"] == T0
    assert store.visible([watching(40)], T0 + 5, HOUR)[0]["seen_at"] == T0
