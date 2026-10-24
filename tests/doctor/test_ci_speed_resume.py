import functools
import json
from collections import Counter
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.doctor import ci_speed_read, loop
from tests.doctor.test_ci_speed import NOW, job
from tests.doctor.test_ci_speed_timer import DOCTOR, WATCHED
from tests.doctor.test_ci_speed_timer import store as store

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(("run_count", "job_count"), [(130, 1), (1, 6100)])
def test_budgeted_passes_resume_jobs_without_losing_active_run_coverage(store, monkeypatch, run_count, job_count):
    calls, reads = [], Counter()
    runs = [{"id": n, "status": "in_progress", "run_attempt": 1, "event": "push"} for n in range(run_count)]

    def api(args, **kwargs):
        endpoint = args[2]
        calls.append(endpoint)
        query = parse_qs(urlsplit(endpoint).query)
        page = int(query["page"][0])
        if "/jobs?" in endpoint:
            reads[endpoint] += 1
            identity = int(urlsplit(endpoint).path.split("/")[-2])
            rows = [
                job(0, identity=identity * job_count + n) for n in range((page - 1) * 100, min(page * 100, job_count))
            ]
            body = {"total_count": job_count, "jobs": rows}
        else:
            selected = runs if "created" in query else []
            body = {"total_count": len(selected), "workflow_runs": selected[(page - 1) * 100 : page * 100]}
        return SimpleNamespace(stdout=json.dumps(body))

    monkeypatch.setattr(ci_speed_read, "read", functools.partial(ci_speed_read.read, run=api))
    state_key = store.key(DOCTOR, "ci-speed")
    reports = []
    for turn in range(4):
        before = len(calls)
        lines = loop.speed(store, DOCTOR, WATCHED, NOW + turn * 60000)
        state = json.loads(store.redis.get(state_key))
        reports.append(state["report"])
        assert len(calls) - before <= 60
        assert all("incomplete" not in line for line in lines)
        if state.get("last") == NOW:
            break
        assert state.get("last", NOW - 60000) == NOW - 60000
    else:
        pytest.fail("saved window did not finish after bounded resumptions")

    assert reports[-1]["runs"] == run_count
    assert reports[-1]["jobs"] == run_count * job_count
    assert reports[-1]["discovered_runs"] == run_count
    assert len(reads) == run_count * ((job_count + 99) // 100)
    assert set(reads.values()) == {1}
    assert sorted(state["pending"]) == list(range(run_count))
    assert state.get("continuation") is None


@pytest.mark.parametrize("status", [403, 503])
def test_request_errors_keep_safe_http_details_in_the_journal(store, monkeypatch, status):
    import subprocess

    calls = []

    def api(args, **kwargs):
        calls.append(args)
        raise subprocess.CalledProcessError(
            1, args, stderr=f"gh: server refused (HTTP {status})\n" + "untrusted diagnostic " * 200
        )

    monkeypatch.setattr(ci_speed_read, "read", functools.partial(ci_speed_read.read, run=api))
    [line] = loop.speed(store, DOCTOR, WATCHED, NOW)
    assert f"HTTP {status}" in line
    assert "repos/the-cloud-clockwork/agentihooks/actions/runs" in line
    assert "untrusted diagnostic" not in line
    assert len(line) < 1000
    assert len(calls) == (2 if status == 503 else 1)
    state = json.loads(store.redis.get(store.key(DOCTOR, "ci-speed")))
    assert state["last"] == NOW - 60000
    assert state["report"]["complete"] is False


def test_a_transient_request_retries_within_the_same_sixty_call_budget(store, monkeypatch):
    import subprocess

    calls = []

    def api(args, **kwargs):
        calls.append(args[2])
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, args, stderr="gh: unavailable (HTTP 503)")
        return SimpleNamespace(stdout=json.dumps({"total_count": 0, "workflow_runs": []}))

    monkeypatch.setattr(ci_speed_read, "read", functools.partial(ci_speed_read.read, run=api))
    [line] = loop.speed(store, DOCTOR, WATCHED, NOW)
    assert line.startswith("doctor CI speed: complete;")
    assert len(calls) == 7
    assert calls[0] == calls[1]
    assert json.loads(store.redis.get(store.key(DOCTOR, "ci-speed")))["last"] == NOW


def test_resuming_a_cached_queue_observation_does_not_invent_elapsed_wait(store, monkeypatch):
    current = [NOW]

    def api(args, **kwargs):
        query = parse_qs(urlsplit(args[2]).query)
        if "/jobs?" in args[2]:
            identity = int(urlsplit(args[2]).path.split("/")[-2])
            row = job(1, queued=current[0] == NOW, created=-60, identity=identity)
            body = {"total_count": 1, "jobs": [row]}
        else:
            selected = (
                [{"id": n, "status": "in_progress", "event": "push"} for n in (1, 2)] if "created" in query else []
            )
            body = {"total_count": len(selected), "workflow_runs": selected}
        return SimpleNamespace(stdout=json.dumps(body))

    monkeypatch.setattr(ci_speed_read, "read", functools.partial(ci_speed_read.read, run=api, budget=(7, 25)))
    loop.speed(store, DOCTOR, WATCHED, NOW)
    current[0] += 20 * 60000
    loop.speed(store, DOCTOR, WATCHED, current[0])
    state = json.loads(store.redis.get(store.key(DOCTOR, "ci-speed")))
    assert state["last"] == NOW
    assert state["report"]["runs"] == 2
    assert state["report"]["crossings"] == 0
    assert store.redis.hlen(store.key(DOCTOR, "doctor-findings")) == 0
