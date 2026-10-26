import json
import subprocess

import pytest

from scripts import ci_gate_queue, ci_gate_shards, ci_test_plan
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
    monkeypatch.setattr(rate_limit, "deadline", None)
    return sleeps


def test_a_wait_never_passes_the_caller_deadline(monkeypatch, clock):
    monkeypatch.setattr(rate_limit, "deadline", rate_limit.time.monotonic() + 30)
    monkeypatch.setattr(rate_limit.subprocess, "run", _gh(iter([LIMITED, b"{}"]), []))
    rate_limit.gh_api(["repos/x"], timeout=60)
    assert len(clock) == 1 and 0 < clock[0] <= 30


def test_a_passed_deadline_raises_without_waiting(monkeypatch, clock):
    monkeypatch.setattr(rate_limit, "deadline", rate_limit.time.monotonic() - 1)
    monkeypatch.setattr(rate_limit.subprocess, "run", _gh(iter([LIMITED]), []))
    with pytest.raises(subprocess.CalledProcessError):
        rate_limit.gh_api(["repos/x"], timeout=60)
    assert clock == []


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


def test_the_last_attempt_keeps_the_caller_timeout_and_text(monkeypatch, clock):
    seen = []
    limited = LIMITED.decode()

    def run(command, **kwargs):
        if command == ["gh", "api", "rate_limit"]:
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"resources": {"core": {"reset": 1120}}}))
        seen.append(kwargs)
        if len(seen) <= rate_limit.WAITS:
            raise subprocess.CalledProcessError(1, command, stderr=limited)
        return subprocess.CompletedProcess(command, 0, stdout="{}")

    monkeypatch.setattr(rate_limit.subprocess, "run", run)
    assert rate_limit.gh_api(["repos/x"], timeout=45, text=True).stdout == "{}"
    assert seen[-1] == {"capture_output": True, "check": True, "timeout": 45, "text": True}
    assert len(seen) == rate_limit.WAITS + 1


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


def test_both_calls_capture_checked_output_with_their_timeouts(monkeypatch, clock):
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        if command == ["gh", "api", "rate_limit"]:
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"resources": {"core": {"reset": 1120}}}))
        if len(seen) == 1:
            raise subprocess.CalledProcessError(1, command, stderr=LIMITED)
        return subprocess.CompletedProcess(command, 0, stdout=b"{}")

    monkeypatch.setattr(rate_limit.subprocess, "run", run)
    rate_limit.gh_api(["repos/x"], timeout=45)
    call = {"capture_output": True, "check": True, "timeout": 45, "text": False}
    assert seen == [
        (["gh", "api", "repos/x"], call),
        (["gh", "api", "rate_limit"], {"capture_output": True, "check": True, "timeout": 30, "text": True}),
        (["gh", "api", "repos/x"], call),
    ]


def test_a_past_reset_waits_one_second(monkeypatch, clock):
    monkeypatch.setattr(rate_limit.time, "time", lambda: 5000.0)
    monkeypatch.setattr(rate_limit.subprocess, "run", _gh(iter([LIMITED, b"{}"]), []))
    rate_limit.gh_api(["repos/x"], timeout=60)
    assert clock == [1.0]


def test_undecodable_stderr_is_still_read(monkeypatch, clock):
    monkeypatch.setattr(rate_limit.subprocess, "run", _gh(iter([b"\xff " + LIMITED, b"{}"]), []))
    rate_limit.gh_api(["repos/x"], timeout=60)
    assert clock == [121]


def test_a_wait_spends_only_the_budget_left(monkeypatch, clock, capsys):
    monkeypatch.setattr(rate_limit, "slept", 800.0)
    monkeypatch.setattr(rate_limit, "deadline", rate_limit.time.monotonic() + 3600)
    monkeypatch.setattr(rate_limit.time, "time", lambda: 0.0)
    monkeypatch.setattr(rate_limit.subprocess, "run", _gh(iter([LIMITED, b"{}"]), []))
    rate_limit.gh_api(["repos/x"], timeout=60)
    assert clock == [100.0]
    assert rate_limit.slept == 900.0
    assert "GitHub API rate limit reached; waiting 100 seconds for the reset\n" in capsys.readouterr().err


def test_report_names_the_caller_and_its_count(monkeypatch, capsys):
    monkeypatch.setattr(rate_limit, "calls", 3)
    rate_limit.report("Queue verification")
    assert capsys.readouterr() == ("", "Queue verification made 3 GitHub API calls\n")


def test_a_wait_under_a_second_before_the_deadline_still_waits(monkeypatch, clock):
    monkeypatch.setattr(rate_limit, "deadline", rate_limit.time.monotonic() + 0.5)
    monkeypatch.setattr(rate_limit.subprocess, "run", _gh(iter([LIMITED, b"{}"]), []))
    rate_limit.gh_api(["repos/x"], timeout=60)
    assert len(clock) == 1 and 0 < clock[0] <= 0.5


BUDGET = "HTTP/2.0 200 OK\nX-Ratelimit-Limit: 5000\nX-Ratelimit-Remaining: 4321\nX-Ratelimit-Reset: 0\n\n{}"


def _budget(monkeypatch, stdout=BUDGET, error=None):
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs))
        if error:
            raise error
        return subprocess.CompletedProcess(command, 0, stdout=stdout)

    monkeypatch.setattr(rate_limit.subprocess, "run", run)
    return commands


def test_budget_prints_the_remaining_calls_and_reset_from_the_headers(monkeypatch, capsys):
    commands = _budget(monkeypatch)
    rate_limit.budget("Queue verification", "start")
    assert commands == [
        (["gh", "api", "--include", "rate_limit"], {"capture_output": True, "check": True, "timeout": 30, "text": True})
    ]
    assert capsys.readouterr() == (
        "",
        "Queue verification start: 4321 of 5000 App API calls left, resets 1970-01-01T00:00:00Z\n",
    )


@pytest.mark.parametrize(
    "stdout, error",
    [
        ("HTTP/2.0 200 OK\nX-Ratelimit-Limit: 5000\n\n{}", None),
        ("HTTP/2.0 200 OK\nX-Ratelimit-Limit: 5000\nX-Ratelimit-Remaining: 1\nX-Ratelimit-Reset: soon\n\n{}", None),
        ("", subprocess.CalledProcessError(1, ["gh"], stderr="gh: Bad credentials (HTTP 401)")),
        ("", subprocess.TimeoutExpired(["gh"], 30)),
    ],
)
def test_an_unreadable_budget_is_named_without_failing_the_gate(monkeypatch, capsys, stdout, error):
    _budget(monkeypatch, stdout, error)
    rate_limit.budget("Shard verification", "end")
    assert capsys.readouterr() == ("", "Shard verification end: App API budget unreadable\n")


def test_budget_reads_header_names_in_any_case(monkeypatch, capsys):
    _budget(monkeypatch, "HTTP/2.0 200 OK\nx-ratelimit-limit: 10\nx-ratelimit-remaining: 2\nx-ratelimit-reset: 60\n\n")
    rate_limit.budget("Shard verification", "start")
    assert (
        capsys.readouterr().err == "Shard verification start: 2 of 10 App API calls left, resets 1970-01-01T00:01:00Z\n"
    )


def test_the_queue_gate_reports_its_calls(monkeypatch, capsys):
    _budget(monkeypatch)
    monkeypatch.setattr(rate_limit, "calls", 4)
    monkeypatch.setattr(ci_gate_queue, "watch", lambda *args: None)
    for name, value in {
        "GITHUB_EVENT_NAME": "merge_group",
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_RUN_ID": "1",
        "GITHUB_RUN_ATTEMPT": "1",
        "QUEUE_PLAN": "[]",
        "GITHUB_REF": "refs/queue",
        "GITHUB_SHA": "head",
    }.items():
        monkeypatch.setenv(name, value)
    ci_gate_queue.main()
    assert capsys.readouterr().err == (
        "Queue verification start: 4321 of 5000 App API calls left, resets 1970-01-01T00:00:00Z\n"
        "Queue verification end: 4321 of 5000 App API calls left, resets 1970-01-01T00:00:00Z\n"
        "Queue verification made 4 GitHub API calls\n"
    )


def test_the_shard_check_reports_its_calls(monkeypatch, capsys):
    _budget(monkeypatch)
    monkeypatch.setattr(rate_limit, "calls", 5)
    monkeypatch.setattr(ci_gate_shards, "watch", lambda *args, **kwargs: None)
    for name, value in {
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_RUN_ID": "1",
        "GITHUB_RUN_ATTEMPT": "1",
        "SHARDS": "[]",
    }.items():
        monkeypatch.setenv(name, value)
    ci_gate_shards.main()
    assert capsys.readouterr().err == (
        "Shard verification start: 4321 of 5000 App API calls left, resets 1970-01-01T00:00:00Z\n"
        "Shard verification end: 4321 of 5000 App API calls left, resets 1970-01-01T00:00:00Z\n"
        "Shard verification made 5 GitHub API calls\n"
    )


def test_the_test_planner_reports_its_calls(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(rate_limit, "calls", 6)
    monkeypatch.setattr(ci_test_plan, "find_record", lambda repository, before: None)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    ci_test_plan.main(["--repository", "o/r", "--head", "h", "--out", str(tmp_path / "plan.json"), "--before", "t"])
    assert capsys.readouterr().err == "The test planner made 6 GitHub API calls\n"
