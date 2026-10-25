import json
import subprocess

import pytest

from scripts import ci_test_plan
from scripts.ci_budget import jobs, rate_limit

LIMITED = b"gh: API rate limit exceeded for installation ID 169395104. (HTTP 403)"


def _gh(responses, commands):
    def run(command, **kwargs):
        commands.append(command)
        if command == ["gh", "api", "rate_limit"]:
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"resources": {"core": {"reset": 1120}}}))
        response = next(responses)
        if "rate limit" in str(response) or "Not Found" in str(response):
            raise subprocess.CalledProcessError(1, command, stderr=response)
        return subprocess.CompletedProcess(command, 0, stdout=response)

    return run


@pytest.fixture
def clock(monkeypatch):
    sleeps = []
    monkeypatch.setattr(rate_limit.time, "time", lambda: 1000.0)
    monkeypatch.setattr(rate_limit.time, "sleep", sleeps.append)
    monkeypatch.setattr(rate_limit, "calls", 0)
    monkeypatch.setattr(rate_limit, "slept", 0.0)
    return sleeps


def test_a_rate_limited_call_waits_for_the_reset_and_retries(monkeypatch, clock):
    commands = []
    monkeypatch.setattr(rate_limit.subprocess, "run", _gh(iter([LIMITED, b"{}"]), commands))
    assert rate_limit.gh_api(["repos/x"], timeout=60).stdout == b"{}"
    assert clock == [121]
    assert commands == [["gh", "api", "repos/x"], ["gh", "api", "rate_limit"], ["gh", "api", "repos/x"]]
    assert rate_limit.calls == 2


def test_a_secondary_rate_limit_waits_a_minute(monkeypatch, clock):
    commands = []
    secondary = b"gh: You have exceeded a secondary rate limit. (HTTP 403)"
    monkeypatch.setattr(rate_limit.subprocess, "run", _gh(iter([secondary, b"{}"]), commands))
    rate_limit.gh_api(["repos/x"], timeout=60)
    assert clock == [60]
    assert ["gh", "api", "rate_limit"] not in commands


def test_an_unreadable_reset_waits_a_minute(monkeypatch, clock):
    def run(command, **kwargs):
        if command == ["gh", "api", "rate_limit"]:
            raise subprocess.TimeoutExpired(command, 30)
        return next(responses)(command)

    def limited(command):
        raise subprocess.CalledProcessError(1, command, stderr=LIMITED)

    responses = iter([limited, lambda command: subprocess.CompletedProcess(command, 0, stdout=b"{}")])
    monkeypatch.setattr(rate_limit.subprocess, "run", run)
    assert rate_limit.gh_api(["repos/x"], timeout=60).stdout == b"{}"
    assert clock == [rate_limit.SECONDARY_WAIT]


def test_other_failures_raise_without_waiting(monkeypatch, clock):
    monkeypatch.setattr(rate_limit.subprocess, "run", _gh(iter([b"gh: Not Found (HTTP 404)"]), []))
    with pytest.raises(subprocess.CalledProcessError):
        rate_limit.gh_api(["repos/x"], timeout=60)
    assert clock == []


def test_a_limit_that_never_clears_raises_after_bounded_waits(monkeypatch, clock):
    commands = []
    monkeypatch.setattr(rate_limit.subprocess, "run", _gh(iter([LIMITED] * (rate_limit.WAITS + 1)), commands))
    with pytest.raises(subprocess.CalledProcessError):
        rate_limit.gh_api(["repos/x"], timeout=60)
    assert clock == [121] * rate_limit.WAITS
    assert commands.count(["gh", "api", "repos/x"]) == rate_limit.WAITS + 1


def test_a_far_reset_waits_at_most_the_cap(monkeypatch, clock):
    monkeypatch.setattr(rate_limit.time, "time", lambda: 0.0)
    monkeypatch.setattr(rate_limit.subprocess, "run", _gh(iter([LIMITED, b"{}"]), []))
    rate_limit.gh_api(["repos/x"], timeout=60)
    assert clock == [rate_limit.MAX_WAIT]


def test_waits_share_one_budget_per_process(monkeypatch, clock):
    monkeypatch.setattr(rate_limit.time, "time", lambda: 0.0)
    monkeypatch.setattr(rate_limit.subprocess, "run", _gh(iter([LIMITED, b"{}", LIMITED]), []))
    rate_limit.gh_api(["repos/x"], timeout=60)
    with pytest.raises(subprocess.CalledProcessError):
        rate_limit.gh_api(["repos/x"], timeout=60)
    assert clock == [rate_limit.MAX_WAIT]


def test_the_job_reader_survives_a_rate_limit(monkeypatch, clock):
    page = json.dumps({"jobs": [{"name": "unit (3.12, 1)"}]})
    monkeypatch.setattr(rate_limit.subprocess, "run", _gh(iter([LIMITED.decode(), page]), []))
    assert jobs.read_jobs("repos/x/jobs", 100) == [{"name": "unit (3.12, 1)"}]
    assert clock == [121]


def test_the_test_planner_waits_out_a_rate_limit_instead_of_running_every_test(monkeypatch, clock):
    monkeypatch.setattr(rate_limit.subprocess, "run", _gh(iter([LIMITED, b'{"workflow_runs": []}']), []))
    assert json.loads(ci_test_plan._api("repos/x/actions/runs")) == {"workflow_runs": []}
    assert clock == [121]
