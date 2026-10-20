import functools
import json
import subprocess
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.doctor import ci_speed_read, cli, loop
from scripts.inbox.store import InboxStore
from scripts.swarm.store import RedisStore, SwarmConfig
from tests.doctor.test_ci_speed import NOW, job, record, stamp

pytestmark = pytest.mark.unit
DOCTOR = "speed-doctor"
WATCHED = "speed-watched"
QUEUED = f'ci-queue-age-unconfirmed/{NOW}-["anton-oss", "self-hosted"]'


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
    jobs = [job(0, queued=True, created=-1000, identity=11)]
    monkeypatch.setattr(ci_speed_read, "read", lambda *args, **kwargs: snapshot([record(jobs)]))
    cli.timer(store, DOCTOR, NOW, False)
    jobs.append(job(0, queued=True, created=-901, identity=12))
    jobs[1]["labels"].reverse()
    cli.timer(store, DOCTOR, NOW + 60000, False)
    details = store.redis.hgetall(store.key(DOCTOR, "doctor-details"))
    assert len(details) == 1
    finding = json.loads(next(iter(details.values())))
    assert finding["summary"].endswith("in 1 run")
    assert "2 jobs" in finding["evidence"][1]
    assert "longest 1060 seconds" in finding["evidence"][1]
    assert "median 1010.5 seconds" in finding["evidence"][1]
    for slug in (DOCTOR, WATCHED):
        assert len(InboxStore(store.redis).inbox(f"master@{slug}")) == 1


def test_every_timer_tick_reads_speed_and_sends_each_crossing_to_both_masters_once(store, monkeypatch):
    calls = []

    def read(since, now, pending=None, known=None, active=True):
        calls.append((since, now, pending))
        return snapshot([record([job(0, queued=True, created=-901)])])

    monkeypatch.setattr(ci_speed_read, "read", read)
    first = cli.timer(store, DOCTOR, NOW, False)
    second = cli.timer(store, DOCTOR, NOW + 60000, False)
    assert calls == [(NOW - 60000, NOW, None), (NOW, NOW + 60000, [7])]
    for slug in [DOCTOR, WATCHED]:
        [item] = InboxStore(store.redis).inbox(f"master@{slug}")
        assert "run 7" in item.text and "anton-oss" in item.text and "901 seconds" in item.text
    assert any("1 runs" in action and "1 jobs" in action and "7 API calls" in action for action in first)
    assert any("CI speed" in action for action in second)
    finding, _ = loop.judged(store, DOCTOR, QUEUED)
    assert finding["measure"] == 961
    assert int(store.redis.hget(store.key(DOCTOR, "doctor-timer"), "last_new")) == NOW
    seen = json.loads(store.redis.hget(store.key(DOCTOR, "doctor-findings"), QUEUED))
    assert seen["seen_at"] == NOW


def test_an_api_failure_reports_incomplete_counts_and_preserves_the_window_for_retry(store, monkeypatch):
    calls = []
    outcomes = [snapshot(), snapshot(complete=False, error="API unavailable"), snapshot()]

    def read(since, now, pending=None, known=None, active=True):
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
    assert (
        "doctor CI speed: complete; 1 runs read of 1 discovered, 1 jobs, 0 crossings, 7 API calls, 0 retracted, 0 queued jobs not reread"
        in actions
    )
    assert InboxStore(store.redis).inbox(f"master@{WATCHED}") == []


def test_a_judged_speed_finding_returns_only_after_its_cooldown(store, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_HEALTH_COOLDOWN_MINUTES", "2")
    monkeypatch.setattr(
        ci_speed_read, "read", lambda *args, **kwargs: snapshot([record([job(0, queued=True, created=-901)])])
    )
    cli.timer(store, DOCTOR, NOW, False)
    judgments = loop.verdicts(store, DOCTOR)
    judgments.judge(QUEUED, "early-real", "runner delay", "reader", NOW)
    cli.timer(store, DOCTOR, NOW + 60000, False)
    first = json.loads(store.redis.hget(judgments.key, QUEUED))
    assert first["returned"] is False
    cli.timer(store, DOCTOR, NOW + 120000, False)
    second = json.loads(store.redis.hget(judgments.key, QUEUED))
    assert second["returned"] is True


def test_persisted_notifications_suppress_a_crossing_after_a_process_restart(store, monkeypatch):
    identity = QUEUED
    for slug in (DOCTOR, WATCHED):
        store.redis.sadd(store.key(DOCTOR, "ci-speed-notified", slug), identity)
    monkeypatch.setattr(
        ci_speed_read, "read", lambda *args, **kwargs: snapshot([record([job(0, queued=True, created=-901)])])
    )
    cli.timer(store, DOCTOR, NOW, False)
    for slug in (DOCTOR, WATCHED):
        assert InboxStore(store.redis).inbox(f"master@{slug}") == []


def test_a_new_speed_crossing_resets_quiet_time_between_regular_detector_passes(store, monkeypatch):
    outcomes = [snapshot(), snapshot([record([job(0, queued=True, created=-901)])])]
    monkeypatch.setattr(ci_speed_read, "read", lambda *args, **kwargs: outcomes.pop(0))
    cli.timer(store, DOCTOR, NOW, False)
    cli.timer(store, DOCTOR, NOW + 60000, False)
    assert int(store.redis.hget(store.key(DOCTOR, "doctor-timer"), "last_new")) == NOW + 60000
    [item] = InboxStore(store.redis).inbox(f"master@{WATCHED}")
    assert "961 seconds" in item.text


def test_jobs_of_finished_runs_carry_over_so_a_window_beyond_the_call_budget_completes(store, monkeypatch):
    runs = [{"id": n, "status": "completed", "run_attempt": 1, "event": "push"} for n in range(100)]
    jobs, down = [], []

    def api(args, **kwargs):
        if down:
            raise subprocess.CalledProcessError(1, args)
        url = urlsplit(args[2])
        if url.path.endswith("/jobs"):
            jobs.append(int(url.path.split("/")[-2]))
            body = {"total_count": 1, "jobs": [{**job(0, created=-30, identity=jobs[-1]), "steps": [{"name": "x"}]}]}
        else:
            selected = runs if "created" in parse_qs(url.query) else []
            body = {"total_count": len(selected), "workflow_runs": selected}
        return SimpleNamespace(stdout=json.dumps(body))

    def saved():
        return json.loads(store.redis.get(store.key(DOCTOR, "ci-speed")))

    monkeypatch.setattr(ci_speed_read, "read", functools.partial(ci_speed_read.read, run=api))
    first = cli.timer(store, DOCTOR, NOW, False)
    assert any("incomplete" in action and "budget exhausted" in action for action in first)
    assert saved()["known"][str(jobs[0])] == {
        "status": "completed",
        "run_attempt": 1,
        "jobs": [{**job(0, created=-30, identity=jobs[0]), "runner_name": None, "conclusion": None}],
    }
    down.append(True)
    cli.timer(store, DOCTOR, NOW + 60000, False)
    assert len(saved()["known"]) == 54
    down.clear()
    third = cli.timer(store, DOCTOR, NOW + 120000, False)
    assert (
        "doctor CI speed: complete; 100 runs read of 100 discovered, 100 jobs, 0 crossings, 52 API calls, 0 retracted, 0 queued jobs not reread"
        in third
    )
    assert sorted(jobs) == list(range(100))
    assert saved()["known"] == {}


def test_a_gap_window_beyond_the_call_budget_is_read_in_slices_until_caught_up(store, monkeypatch):
    gap = 200 * 60000
    clock = [NOW]
    active = [
        {"id": 10**6 + n, "status": "in_progress", "run_attempt": 1, "event": "push", "created_at": stamp(-30000)}
        for n in range(42)
    ]

    def created():
        runs, n = [], 0
        while NOW - gap + n * 12000 <= clock[0]:
            runs.append(
                {
                    "id": n,
                    "status": "completed",
                    "run_attempt": 1,
                    "event": "push",
                    "created_at": stamp(n * 12 - gap // 1000),
                }
            )
            n += 1
        return runs

    def api(args, **kwargs):
        url = urlsplit(args[2])
        query = parse_qs(url.query)
        identity = url.path.split("/")[-1]
        if url.path.endswith("/jobs"):
            return SimpleNamespace(
                stdout=json.dumps({"total_count": 1, "jobs": [job(0, created=-30, identity=url.path.split("/")[-2])]})
            )
        if identity.isdigit():
            [found] = [run for run in active + created() if run["id"] == int(identity)]
            return SimpleNamespace(stdout=json.dumps(found))
        if "created" in query:
            since, until = (datetime.fromisoformat(edge) for edge in query["created"][0].split(".."))
            selected = [run for run in created() if since <= datetime.fromisoformat(run["created_at"]) <= until]
        else:
            selected = active if query["status"] == ["in_progress"] else []
        page = int(query["page"][0])
        body = {"total_count": len(selected), "workflow_runs": selected[(page - 1) * 100 : page * 100]}
        return SimpleNamespace(stdout=json.dumps(body))

    store.redis.set(store.key(DOCTOR, "ci-speed"), json.dumps({"last": NOW - gap}))
    monkeypatch.setattr(ci_speed_read, "read", functools.partial(ci_speed_read.read, run=api))
    reports, statuses = [], []
    for minute in range(40):
        clock[0] = NOW + minute * 60000
        statuses += [action for action in cli.timer(store, DOCTOR, clock[0], False) if "CI speed" in action]
        reports.append(json.loads(store.redis.get(store.key(DOCTOR, "ci-speed")))["report"])
    complete = [report["complete"] for report in reports]
    assert statuses[0].startswith("doctor CI speed: catching up;") and complete[0] is False
    assert statuses[-1].startswith("doctor CI speed: complete;")
    assert min(start for start in range(len(complete) + 1) if all(complete[start:])) < 35
    runs_in_one_slice = loop.SPEED_SLICE_MINUTES * 5 + 1
    assert max(report["discovered_runs"] for report in reports) <= runs_in_one_slice + len(active)
    assert json.loads(store.redis.get(store.key(DOCTOR, "ci-speed")))["last"] == clock[0]
