import json
from datetime import datetime, timezone

import pytest

from scripts.doctor import cli, loop
from scripts.swarm.health import verdicts
from scripts.swarm.health.findings import Finding
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError

DAY = 86400000
START = int(datetime(2026, 10, 23, tzinfo=timezone.utc).timestamp() * 1000)
DOCTOR = "watched-doctor"


@pytest.fixture
def store():
    from fakeredis import FakeRedis

    result = RedisStore(FakeRedis(decode_responses=True))
    result.create(SwarmConfig("watched", "/repo", 1, 1))
    result.create(SwarmConfig(DOCTOR, "/repo", 1, 1, template=cli.TEMPLATE))
    return result


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


def test_removing_a_doctor_removes_its_verdict_events(store):
    judgments, found = finding(store)
    judgments.judge(found.id, "resolved", "done", "judge", START)
    assert len(judgments.events(START, START + 1, START + 1)) == 1
    store.remove(DOCTOR)
    assert judgments.events(START, START + 1, START + 1) == []


def test_the_doctor_reader_finds_events_written_to_the_retained_log_key(store):
    existing = verdicts.VerdictStore(
        store.redis, store.key(DOCTOR, "doctor-findings"), store.key(DOCTOR, "doctor-verdicts")
    )
    found = Finding("test", "existing", "Existing finding", ("evidence",), "above zero", 7)
    existing.visible([found], START, DAY)
    existing.judge(found.id, "early-real", "retained", "judge", START)
    assert loop.verdicts(store, DOCTOR).events(START, START + 1, START + 1) == [
        {
            "finding": "test/existing",
            "value": "early-real",
            "note": "retained",
            "by": "judge",
            "at": START,
            "measure": 7,
        }
    ]


def test_ordinary_finding_verdicts_keep_the_existing_keys_without_an_event_log(store):
    judgments = verdicts.VerdictStore(store.redis, store.key("watched", "findings"))
    found = Finding("test", "ordinary", "Ordinary finding", ("evidence",), "above zero", 1)
    judgments.visible([found], START, DAY)
    before = set(store.redis.scan_iter())
    judgments.judge(found.id, "resolved", "done", "judge", START)
    assert set(store.redis.scan_iter()) == before
    assert json.loads(store.redis.hget(judgments.key, found.id))["verdict"]["value"] == "resolved"


def test_events_have_a_seven_day_bound_and_an_exclusive_window_end(store):
    judgments, found = finding(store)
    for at in (START - 7 * DAY - 1, START - 7 * DAY, START - 1, START):
        judgments.judge(found.id, "resolved", str(at), "judge", at)
    events = judgments.events(START - 7 * DAY, START, START)
    assert [event["at"] for event in events] == [START - 7 * DAY, START - 1]
    assert store.redis.zcard(judgments.event_key) == 3
    assert 7 * DAY - 10000 <= store.redis.pttl(judgments.event_key) <= 7 * DAY
    with pytest.raises(SwarmError) as exc:
        judgments.events(START - 7 * DAY - 1, START, START)
    assert str(exc.value) == "verdict log window exceeds seven day retention"


@pytest.mark.parametrize("since,until", [(START, START), (START + 1, START), (START, START + 2)])
def test_event_reader_refuses_reversed_or_future_windows(store, since, until):
    judgments, _ = finding(store)
    with pytest.raises(SwarmError) as exc:
        judgments.events(since, until, START + 1)
    assert str(exc.value) == "verdict log needs --since before --until and --until no later than now"


@pytest.mark.parametrize("flag", ["--since", "--until"])
def test_verdict_log_names_the_invalid_time_option(store, monkeypatch, capsys, flag):
    monkeypatch.setattr(cli.swarm, "connect", lambda: store)
    monkeypatch.setattr(cli.swarm, "now_ms", lambda: START + DAY)
    options = {"--since": "2026-10-23T00:00Z", "--until": "2026-10-24T00:00Z"}
    options[flag] = "bad"
    args = ["watched", "verdict-log", *[part for item in options.items() for part in item]]
    assert cli.main(args) == 1
    assert capsys.readouterr().err == f"doctor: {flag} takes an ISO time such as 2026-10-06T12:00Z, not bad\n"


@pytest.mark.parametrize("bounds", [[], ["--since", "2026-10-23T00:00Z"], ["--until", "2026-10-24T00:00Z"]])
def test_verdict_log_requires_both_window_bounds(bounds, store, monkeypatch):
    monkeypatch.setattr(cli.swarm, "connect", lambda: store)
    with pytest.raises(SystemExit) as exc:
        cli.main(["watched", "verdict-log", *bounds])
    assert exc.value.code == 2


@pytest.mark.parametrize("slug", ["watched", DOCTOR])
def test_verdict_log_counts_all_events_beside_latest_records_for_a_day(store, monkeypatch, capsys, slug):
    judgments, found = finding(store)
    judgments.judge(found.id, "false-positive", "first", "judge", START)
    judgments.judge(found.id, "false-positive", "again", "judge", START)
    for subject, at in (("before", START - 1), ("after", START + DAY), ("pending", None)):
        extra = Finding("test", subject, "Other finding", ("evidence",), "above zero", 1)
        judgments.visible([extra], START, DAY)
        if at is not None:
            judgments.judge(extra.id, "resolved", subject, "judge", at)
    monkeypatch.setattr(cli.swarm, "connect", lambda: store)
    monkeypatch.setattr(cli.swarm, "now_ms", lambda: START + DAY)
    assert cli.main([slug, "verdict-log", "--since", "2026-10-23T00:00Z", "--until", "2026-10-24T00:00Z"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["since"] == START
    assert report["until"] == START + DAY
    assert report["event_count"] == 2
    assert report["latest_count"] == 1
    assert report["events_by_value"] == {"false-positive": 2}
    assert report["latest_by_value"] == {"false-positive": 1}
    assert len(report["events"]) == 2
