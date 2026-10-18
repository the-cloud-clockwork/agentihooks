import json

import pytest

from scripts.doctor import ci_speed_read, cli, loop
from scripts.inbox.store import InboxStore
from scripts.swarm.store import RedisStore, SwarmConfig
from tests.doctor.test_ci_speed import NOW, job, record

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]
DOCTOR = "speed-doctor"
WATCHED = "speed-watched"


@pytest.fixture
def store(monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(DOCTOR, "/repo", 1, 0, state="drained", template="doctor"))
    store.set_peer(DOCTOR, WATCHED)
    monkeypatch.setattr(cli.detect, "readers", lambda *args, **kwargs: {})
    return store


def snapshot(records=(), **extra):
    return {"complete": True, "runs": list(records), "api_calls": 7, "discovered_runs": len(records), **extra}


def test_later_waiting_jobs_grow_one_finding_without_another_notice(store, monkeypatch):
    jobs = [job(0, queued=True, created=-400, identity=11)]
    monkeypatch.setattr(ci_speed_read, "read", lambda *args, **kwargs: snapshot([record(jobs)]))
    cli.timer(store, DOCTOR, NOW, False)
    jobs.append(job(0, queued=True, created=-301, identity=12))
    jobs[1]["labels"].reverse()
    cli.timer(store, DOCTOR, NOW + 60000, False)
    details = store.redis.hgetall(store.key(DOCTOR, "doctor-details"))
    assert len(details) == 1
    finding = json.loads(next(iter(details.values())))
    assert "2 jobs" in finding["summary"]
    assert "longest 460 seconds" in finding["evidence"][0]
    assert "median 410.5 seconds" in finding["evidence"][0]
    for slug in (DOCTOR, WATCHED):
        assert len(InboxStore(store.redis).inbox(f"master@{slug}")) == 1


def test_every_timer_tick_reads_speed_and_sends_each_crossing_to_both_masters_once(store, monkeypatch):
    calls = []

    def read(since, now, pending=None):
        calls.append((since, now, pending))
        return snapshot([record([job(0, queued=True, created=-301)])])

    monkeypatch.setattr(ci_speed_read, "read", read)
    first = cli.timer(store, DOCTOR, NOW, False)
    second = cli.timer(store, DOCTOR, NOW + 60000, False)
    assert calls == [(NOW - 60000, NOW, None), (NOW, NOW + 60000, [7])]
    for slug in [DOCTOR, WATCHED]:
        [item] = InboxStore(store.redis).inbox(f"master@{slug}")
        assert "run 7" in item.text and "anton-oss" in item.text and "301 seconds" in item.text
    assert any("1 runs" in action and "1 jobs" in action and "7 API calls" in action for action in first)
    assert any("CI speed" in action for action in second)
    finding, _ = loop.judged(store, DOCTOR, 'ci-runner-wait/7-1-["anton-oss", "self-hosted"]')
    assert finding["measure"] == 361
    assert int(store.redis.hget(store.key(DOCTOR, "doctor-timer"), "last_new")) == NOW
    seen = json.loads(
        store.redis.hget(store.key(DOCTOR, "doctor-findings"), 'ci-runner-wait/7-1-["anton-oss", "self-hosted"]')
    )
    assert seen["seen_at"] == NOW


def test_an_api_failure_reports_incomplete_counts_and_preserves_the_window_for_retry(store, monkeypatch):
    calls = []
    outcomes = [snapshot(), snapshot(complete=False, error="API unavailable"), snapshot()]

    def read(since, now, pending=None):
        calls.append((since, pending))
        return outcomes.pop(0)

    monkeypatch.setattr(ci_speed_read, "read", read)
    cli.timer(store, DOCTOR, NOW, False)
    actions = cli.timer(store, DOCTOR, NOW + 60000, False)
    cli.timer(store, DOCTOR, NOW + 120000, False)
    assert calls == [(NOW - 60000, None), (NOW, []), (NOW, [])]
    assert any("incomplete" in action and "API unavailable" in action for action in actions)
    [item] = InboxStore(store.redis).inbox(f"master@{WATCHED}")
    assert "API unavailable" in item.text
    assert item.text.startswith("New Doctor findings on the swarm speed-watched:")
    finding, _ = loop.judged(store, DOCTOR, "ci-speed-read/agentihooks")
    assert finding == {
        "kind": "ci speed read",
        "subject": "agentihooks",
        "summary": "CI speed API read is incomplete",
        "evidence": ["API unavailable"],
        "threshold": "Actions API read must complete",
        "id": "ci-speed-read/agentihooks",
        "measure": 1,
    }


def test_stopped_or_unlinked_doctors_do_not_read_the_api(store, monkeypatch):
    monkeypatch.setattr(ci_speed_read, "read", lambda *args, **kwargs: pytest.fail("unexpected API read"))
    store.update(DOCTOR, state="stopped")
    assert cli.timer(store, DOCTOR, NOW, False) == []
    store.update(DOCTOR, state="drained")
    store.clear_peer(DOCTOR)
    assert cli.timer(store, DOCTOR, NOW, False) == []


def test_a_complete_pass_records_full_counts_and_the_next_active_set(store, monkeypatch):
    monkeypatch.setattr(ci_speed_read, "read", lambda *args, **kwargs: snapshot([record([], status="completed")]))
    cli.timer(store, DOCTOR, NOW, False)
    saved = json.loads(store.redis.get(store.key(DOCTOR, "ci-speed")))
    assert saved["last"] == NOW and saved["pending"] == []
    assert saved["report"]["runs"] == 1 and saved["report"]["jobs"] == 0


def test_the_timer_excludes_jobs_that_started_before_the_saved_window(store, monkeypatch):
    monkeypatch.setattr(ci_speed_read, "read", lambda *args, **kwargs: snapshot([record([job(301, created=-601)])]))
    actions = cli.timer(store, DOCTOR, NOW, False)
    assert "doctor CI speed: complete; 1 runs read of 1 discovered, 1 jobs, 0 crossings, 7 API calls" in actions
    assert InboxStore(store.redis).inbox(f"master@{WATCHED}") == []


def test_a_judged_speed_finding_returns_only_after_its_cooldown(store, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_HEALTH_COOLDOWN_MINUTES", "2")
    monkeypatch.setattr(
        ci_speed_read, "read", lambda *args, **kwargs: snapshot([record([job(0, queued=True, created=-301)])])
    )
    cli.timer(store, DOCTOR, NOW, False)
    judgments = loop.verdicts(store, DOCTOR)
    judgments.judge('ci-runner-wait/7-1-["anton-oss", "self-hosted"]', "early-real", "runner delay", "reader", NOW)
    cli.timer(store, DOCTOR, NOW + 60000, False)
    first = json.loads(store.redis.hget(judgments.key, 'ci-runner-wait/7-1-["anton-oss", "self-hosted"]'))
    assert first["returned"] is False
    cli.timer(store, DOCTOR, NOW + 120000, False)
    second = json.loads(store.redis.hget(judgments.key, 'ci-runner-wait/7-1-["anton-oss", "self-hosted"]'))
    assert second["returned"] is True


def test_persisted_notifications_suppress_a_crossing_after_a_process_restart(store, monkeypatch):
    identity = 'ci-runner-wait/7-1-["anton-oss", "self-hosted"]'
    for slug in (DOCTOR, WATCHED):
        store.redis.sadd(store.key(DOCTOR, "ci-speed-notified", slug), identity)
    monkeypatch.setattr(
        ci_speed_read, "read", lambda *args, **kwargs: snapshot([record([job(0, queued=True, created=-301)])])
    )
    cli.timer(store, DOCTOR, NOW, False)
    for slug in (DOCTOR, WATCHED):
        assert InboxStore(store.redis).inbox(f"master@{slug}") == []


def test_a_new_speed_crossing_resets_quiet_time_between_regular_detector_passes(store, monkeypatch):
    outcomes = [snapshot(), snapshot([record([job(0, queued=True, created=-301)])])]
    monkeypatch.setattr(ci_speed_read, "read", lambda *args, **kwargs: outcomes.pop(0))
    cli.timer(store, DOCTOR, NOW, False)
    cli.timer(store, DOCTOR, NOW + 60000, False)
    assert int(store.redis.hget(store.key(DOCTOR, "doctor-timer"), "last_new")) == NOW + 60000
    [item] = InboxStore(store.redis).inbox(f"master@{WATCHED}")
    assert "361 seconds" in item.text
