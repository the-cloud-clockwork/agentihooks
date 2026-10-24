import functools
import json
from collections import Counter
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.doctor import ci_speed_read, loop
from tests.doctor.test_ci_speed import NOW, job
from tests.doctor.test_ci_speed_timer import DOCTOR, WATCHED

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
