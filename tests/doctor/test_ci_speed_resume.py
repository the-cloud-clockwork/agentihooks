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
    outside = {"id": 1000000, "status": "in_progress", "run_attempt": 1, "event": "push"}

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
            selected = runs if "created" in query else [outside] if query["status"] == ["in_progress"] else []
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

    assert reports[-1]["runs"] == run_count + 1
    assert reports[-1]["jobs"] == (run_count + 1) * job_count
    assert reports[-1]["discovered_runs"] == run_count + 1
    assert len(reads) == (run_count + 1) * ((job_count + 99) // 100)
    assert set(reads.values()) == {1}
    assert sorted(state["pending"]) == [*range(run_count), 1000000]
    assert state.get("continuation") is None


@pytest.mark.parametrize("status", [403, 502, 503, 504])
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
    assert len(calls) == (1 if status == 403 else 2)
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


def test_retrying_an_incomplete_list_refreshes_its_retained_page():
    reads = []
    responses = {}

    def api(args, **kwargs):
        reads.append(args[2])
        if len(reads) == 1:
            body = {"total_count": 2, "workflow_runs": []}
        elif "created=" in args[2]:
            body = {"total_count": 1, "workflow_runs": [{"id": 7, "status": "completed", "event": "push"}]}
        elif "/jobs?" in args[2]:
            body = {"total_count": 1, "jobs": [job(0)]}
        else:
            body = {"total_count": 0, "workflow_runs": []}
        return SimpleNamespace(stdout=json.dumps(body))

    result = ci_speed_read.read(NOW - 60000, NOW, pending=ci_speed_read.Progress([], responses), run=api)
    assert result["complete"] is True
    assert [run["id"] for run in result["runs"]] == [7]
    assert result["runs"][0]["jobs"] == [job(0)]
    assert result["api_calls"] == len(reads) == 8
    assert reads[0] == reads[1]


def test_a_legacy_finished_run_cache_survives_the_first_resumable_pass(store, monkeypatch):
    from tests.doctor.test_ci_speed_read import api

    run, calls = api(records=[{"id": 7, "status": "completed", "run_attempt": 2, "event": "push"}])
    state_key = store.key(DOCTOR, "ci-speed")
    kept = {"status": "completed", "run_attempt": 2, "jobs": [job(0)]}
    store.redis.set(state_key, json.dumps({"last": NOW - 60000, "known": {"7": kept}}))
    monkeypatch.setattr(ci_speed_read, "read", functools.partial(ci_speed_read.read, run=run))
    loop.speed(store, DOCTOR, WATCHED, NOW)
    state = json.loads(store.redis.get(state_key))
    assert state["report"]["complete"] is True
    assert state["report"]["jobs"] == 1
    assert state["report"]["runs"] == 1
    assert state["report"]["api_calls"] == len(calls) == 6
    assert state["last"] == NOW
    assert state["known"] == {}
