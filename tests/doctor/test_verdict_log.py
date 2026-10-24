import json
from datetime import datetime, timezone

import pytest

from scripts.doctor import cli, loop
from scripts.swarm.health import verdicts
from scripts.swarm.health.findings import Finding
from scripts.swarm.store import RedisStore

DAY = 86400000
START = int(datetime(2026, 10, 23, tzinfo=timezone.utc).timestamp() * 1000)
DOCTOR = "watched-doctor"


@pytest.fixture
def store():
    from fakeredis import FakeRedis

    return RedisStore(FakeRedis(decode_responses=True))


def finding(store, doctor=DOCTOR):
    judgments = loop.verdicts(store, doctor)
    assert isinstance(judgments, verdicts.VerdictStore)
    found = Finding("test", "repeat", "Repeated finding", ("evidence",), "above zero", 3)
    judgments.visible([found], START, DAY)
    return judgments, found


def test_every_judgment_is_recoverable_and_latest_still_controls_cooldown(store):
    judgments, found = finding(store)
    judgments.judge(found.id, "false-positive", "first note", "first judge", START + 1)
    judgments.judge(found.id, "established", "second note", "second judge", START + 2)
    events = judgments.events(START, START + DAY, START + DAY)
    assert events == [
        {
            "finding": "test/repeat",
            "value": "false-positive",
            "note": "first note",
            "by": "first judge",
            "at": START + 1,
            "measure": 3,
        },
        {
            "finding": "test/repeat",
            "value": "established",
            "note": "second note",
            "by": "second judge",
            "at": START + 2,
            "measure": 3,
        },
    ]
    current = json.loads(store.redis.hget(judgments.key, found.id))["verdict"]
    assert current["value"] == "established"
    assert current["at"] == START + 2
    grown = Finding("test", "repeat", "Repeated finding", ("more evidence",), "above zero", 4)
    assert judgments.visible([grown], START + DAY + 1, DAY) == []
    assert judgments.visible([grown], START + DAY + 2, DAY)[0]["verdict"]["note"] == "second note"


def test_identical_judgments_in_one_millisecond_remain_separate_and_doctors_are_isolated(store):
    judgments, found = finding(store)
    for _ in range(2):
        judgments.judge(found.id, "false-positive", "same", "judge", START)
    other, _ = finding(store, "other-doctor")
    assert len(judgments.events(START, START + 1, START + 1)) == 2
    assert other.events(START, START + 1, START + 1) == []


def test_verdict_log_counts_all_events_beside_latest_records_for_a_day(store, monkeypatch, capsys):
    judgments, found = finding(store)
    judgments.judge(found.id, "false-positive", "first", "judge", START + 1)
    judgments.judge(found.id, "false-positive", "again", "judge", START + 2)
    monkeypatch.setattr(cli.swarm, "connect", lambda: store)
    monkeypatch.setattr(cli.swarm, "now_ms", lambda: START + DAY)
    assert cli.main(["watched", "verdict-log", "--since", "2026-10-23T00:00Z", "--until", "2026-10-24T00:00Z"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["event_count"] == 2
    assert report["latest_count"] == 1
    assert report["events_by_value"] == {"false-positive": 2}
    assert report["latest_by_value"] == {"false-positive": 1}
    assert len(report["events"]) == 2
