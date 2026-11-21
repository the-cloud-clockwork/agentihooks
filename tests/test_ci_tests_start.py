import json
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

LISTING = "repos/owner/repo/actions/workflows/test.yml/runs?head_sha=head&event=pull_request&per_page=1"
CHECKS = "repos/owner/repo/commits/head/check-runs?per_page=100"
LINK = "https://github.com/owner/repo/actions/runs/{}/job/{}"
GITHUB_CALL_SECONDS = 0.4


def _verify(monkeypatch: pytest.MonkeyPatch, runs: list[dict[str, Any]], jobs: dict[str, Any]) -> tuple[str, list[str]]:
    from scripts import ci_tests_start

    calls = []

    def api(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        endpoint = args[0][-1]
        calls.append(endpoint)
        payload = jobs if "/jobs?" in endpoint else {"workflow_runs": runs, "check_runs": []}
        return subprocess.CompletedProcess(args[0], 0, json.dumps(payload), "")

    monkeypatch.setattr(ci_tests_start.subprocess, "run", api)
    monkeypatch.setattr(ci_tests_start.time, "sleep", lambda _: None)
    return ci_tests_start.verify("owner/repo", "head", attempts=2, interval=0), calls


@pytest.mark.parametrize(
    "runs,jobs,message",
    [
        ([], {"total_count": 0}, "missing"),
        (
            [{"id": 11, "head_sha": "head", "status": "completed", "conclusion": "failure"}],
            {"total_count": 0},
            "failure",
        ),
        (
            [{"id": 11, "head_sha": "head", "status": "completed", "conclusion": "success"}],
            {"total_count": 0},
            "no jobs",
        ),
        (
            [{"id": 11, "head_sha": "other", "status": "completed", "conclusion": "success"}],
            {"total_count": 1},
            "missing",
        ),
    ],
)
def test_rejects_tests_without_startup_evidence(monkeypatch, runs, jobs, message):
    result, _ = _verify(monkeypatch, runs, jobs)
    assert (
        result
        == {
            "missing": "Tests run is missing for this head.",
            "failure": "Tests run 11 ended with failure.",
            "no jobs": "Tests run 11 has no jobs.",
        }[message]
    )


@pytest.mark.parametrize("status,conclusion", [("in_progress", None), ("completed", "success")])
def test_accepts_tests_with_jobs_for_the_exact_head(monkeypatch, status, conclusion):
    result, calls = _verify(
        monkeypatch,
        [
            {"id": 11, "head_sha": "head", "status": status, "conclusion": conclusion},
        ],
        {"total_count": 1},
    )
    assert result == ""
    assert calls == [
        "repos/owner/repo/actions/workflows/test.yml/runs?head_sha=head&event=pull_request&per_page=1",
        "repos/owner/repo/actions/runs/11/jobs?filter=latest&per_page=1",
    ]


def test_api_failure_cannot_pass(monkeypatch):
    from scripts import ci_tests_start

    def failed(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(ci_tests_start.subprocess, "run", failed)
    with pytest.raises(subprocess.CalledProcessError):
        ci_tests_start.verify("owner/repo", "head", attempts=1, interval=0)


@pytest.mark.parametrize(
    "problem,code,output",
    [("", 0, "Tests startup verified for this head.\n"), ("missing", 1, "::error::missing\n")],
    ids=["started", "missing"],
)
def test_command_reports_startup_result(monkeypatch, capsys, problem, code, output):
    from scripts import ci_tests_start

    calls = []
    monkeypatch.setattr(ci_tests_start, "verify", lambda *args, **kwargs: calls.append((args, kwargs)) or problem)
    assert ci_tests_start.main(["--repository", "owner/repo", "--head", "head"]) == code
    assert calls == [(("owner/repo", "head"), {"attempts": 36, "event": "pull_request", "waits": 36})]
    assert capsys.readouterr().out == output


def test_waits_for_a_run_and_its_first_job(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import ci_tests_start

    replies = iter(
        [
            {"workflow_runs": []},
            {"check_runs": []},
            {"workflow_runs": [{"id": 11, "head_sha": "head", "status": "queued", "conclusion": None}]},
            {"total_count": 0},
            {"workflow_runs": [{"id": 11, "head_sha": "head", "status": "in_progress", "conclusion": None}]},
            {"total_count": 1},
        ]
    )
    sleeps = []
    monkeypatch.setattr(
        ci_tests_start.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, json.dumps(next(replies)), ""),
    )
    monkeypatch.setattr(ci_tests_start.time, "sleep", sleeps.append)
    assert ci_tests_start.verify("owner/repo", "head", attempts=3, interval=2) == ""
    assert sleeps == [2, 2]


def test_does_not_sleep_after_final_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import ci_tests_start

    monkeypatch.setattr(
        ci_tests_start.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, '{"workflow_runs": [], "check_runs": []}', ""),
    )
    monkeypatch.setattr(ci_tests_start.time, "sleep", lambda _: pytest.fail("slept after final attempt"))
    assert ci_tests_start.verify("owner/repo", "head", attempts=1) == "Tests run is missing for this head."


def test_api_uses_checked_bounded_json_request(monkeypatch):
    from scripts import ci_tests_start

    calls = []

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, '{"key": "value"}', "")

    monkeypatch.setattr(ci_tests_start.subprocess, "run", request)
    assert ci_tests_start._api("endpoint") == {"key": "value"}
    assert calls == [
        ((["gh", "api", "endpoint"],), {"check": True, "capture_output": True, "text": True, "timeout": 30})
    ]


def test_default_registration_wait_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import ci_tests_start

    calls = []
    sleeps = []

    def api(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args[0][-1])
        return subprocess.CompletedProcess(args[0], 0, '{"workflow_runs": [], "check_runs": []}', "")

    monkeypatch.setattr(ci_tests_start.subprocess, "run", api)
    monkeypatch.setattr(ci_tests_start.time, "sleep", sleeps.append)
    assert ci_tests_start.verify("owner/repo", "head") == "Tests run is missing for this head."
    assert calls == [LISTING, CHECKS] * 36
    assert sleeps == [5] * 35


@pytest.mark.parametrize("arguments", [[], ["--repository", "owner/repo"], ["--head", "head"]])
def test_command_requires_repository_and_head(monkeypatch, arguments):
    from scripts import ci_tests_start

    monkeypatch.setattr(ci_tests_start, "verify", lambda *args, **kwargs: pytest.fail("accepted missing arguments"))
    with pytest.raises(SystemExit) as stopped:
        ci_tests_start.main(arguments)
    assert stopped.value.code == 2


def test_can_verify_the_push_event_used_by_the_ci_plant(monkeypatch, capsys):
    from scripts import ci_tests_start

    endpoints = []

    def api(endpoint):
        assert endpoint == "repos/owner/repo/actions/workflows/test.yml/runs?head_sha=head&event=push&per_page=1"
        endpoints.append(endpoint)
        return {"workflow_runs": [{"id": 11, "head_sha": "head", "status": "completed", "conclusion": "failure"}]}

    monkeypatch.setattr(ci_tests_start, "_api", api)
    assert ci_tests_start.main(["--repository", "owner/repo", "--head", "head", "--event", "push"]) == 1
    assert endpoints == ["repos/owner/repo/actions/workflows/test.yml/runs?head_sha=head&event=push&per_page=1"]
    assert capsys.readouterr().out == "::error::Tests run 11 ended with failure.\n"


def _replay(monkeypatch: pytest.MonkeyPatch, replies: list[dict]) -> tuple[ModuleType, list[str], list[float]]:
    from scripts import ci_tests_start

    calls: list[str] = []
    sleeps: list[float] = []
    queue = iter(replies)

    def api(endpoint: str) -> dict:
        calls.append(endpoint)
        return next(queue)

    monkeypatch.setattr(ci_tests_start, "_api", api)
    monkeypatch.setattr(ci_tests_start.time, "sleep", sleeps.append)
    return ci_tests_start, calls, sleeps


def test_keeps_waiting_past_registration_while_the_run_lists_jobs_late(monkeypatch: pytest.MonkeyPatch) -> None:
    queued = {"workflow_runs": [{"id": 11, "head_sha": "head", "status": "queued", "conclusion": None}]}
    module, calls, sleeps = _replay(
        monkeypatch,
        [
            queued,
            {"total_count": 0},
            queued,
            {"total_count": 0},
            queued,
            {"total_count": 0},
            queued,
            {"total_count": 1},
        ],
    )
    assert module.verify("owner/repo", "head", attempts=1, interval=3, waits=5) == ""
    assert len(calls) == 8
    assert sleeps == [3, 3, 3]


def test_a_run_that_completes_without_jobs_is_red_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    done = {"workflow_runs": [{"id": 11, "head_sha": "head", "status": "completed", "conclusion": "success"}]}
    module, calls, sleeps = _replay(monkeypatch, [done, {"total_count": 0}])
    assert module.verify("owner/repo", "head", attempts=5, interval=3, waits=5) == "Tests run 11 has no jobs."
    assert len(calls) == 2
    assert sleeps == []


def test_a_run_that_never_lists_jobs_is_red_after_the_wait_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    running = {"workflow_runs": [{"id": 11, "head_sha": "head", "status": "in_progress", "conclusion": None}]}
    module, calls, sleeps = _replay(monkeypatch, [running, {"total_count": 0}] * 3)
    assert module.verify("owner/repo", "head", attempts=1, interval=3, waits=3) == "Tests run 11 has no jobs."
    assert len(calls) == 6
    assert sleeps == [3, 3]


def test_zero_polls_report_the_missing_run_without_calling_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    module, calls, sleeps = _replay(monkeypatch, [])
    assert module.verify("owner/repo", "head", attempts=0, interval=3, waits=0) == "Tests run is missing for this head."
    assert calls == []
    assert sleeps == []


def test_default_wait_for_a_started_run_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    running = {"workflow_runs": [{"id": 11, "head_sha": "head", "status": "queued", "conclusion": None}]}
    module, calls, sleeps = _replay(monkeypatch, [running, {"total_count": 0}] * 36)
    assert module.verify("owner/repo", "head") == "Tests run 11 has no jobs."
    assert len(calls) == 72
    assert sleeps == [5] * 35


def test_command_passes_the_started_run_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import ci_tests_start

    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def verify(*args: str, **kwargs: object) -> str:
        calls.append((args, kwargs))
        return ""

    monkeypatch.setattr(ci_tests_start, "verify", verify)
    assert ci_tests_start.main(["--repository", "owner/repo", "--head", "head", "--waits", "120"]) == 0
    assert calls == [(("owner/repo", "head"), {"attempts": 120, "event": "pull_request", "waits": 120})]


def _replay_grade_job(
    monkeypatch: pytest.MonkeyPatch,
    start: float,
    reply: Callable[[str, float], dict[str, Any]],
    latency: float = 0,
) -> tuple[int, list[str], float]:
    """Run the Gate Protected grade job's ci_tests_start command on a clock its sleeps and GitHub calls advance.

    The clock starts at ``start`` s, each GitHub call takes ``latency`` s and ``reply`` answers an endpoint at the
    current time. Return the exit code, the endpoints called and the seconds left before the job's timeout.
    """
    from scripts import ci_tests_start

    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/gate-protected.yml"
    grade = yaml.safe_load(workflow.read_text())["jobs"]["grade"]
    line = next(
        text for step in grade["steps"] for text in step.get("run", "").splitlines() if "scripts.ci_tests_start" in text
    )
    names = {"$GITHUB_REPOSITORY": "owner/repo", "$HEAD_SHA": "head"}
    words = shlex.split(line)
    arguments = [
        names[word] if word.startswith("$") else word for word in words[words.index("scripts.ci_tests_start") + 1 :]
    ]
    calls: list[str] = []
    now = start

    def api(endpoint: str) -> dict[str, Any]:
        nonlocal now
        now += latency
        calls.append(endpoint)
        return reply(endpoint, now)

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr(ci_tests_start, "_api", api)
    monkeypatch.setattr(ci_tests_start.time, "sleep", sleep)
    code = ci_tests_start.main(arguments)
    return code, calls, grade["timeout-minutes"] * 60 - now


@pytest.mark.feature("gate-protected-waits-out-a-held-tests-run")
@pytest.mark.parametrize("status", ["queued", "in_progress", "pending"])
def test_the_grade_job_waits_out_a_tests_run_held_behind_a_cancelled_run(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """Polling from 21 s into the grade job, a Tests run that lists its first job at 328 s passes before the timeout."""

    def reply(endpoint: str, now: float) -> dict[str, Any]:
        if "/jobs?" in endpoint:
            return {"total_count": int(now >= 328)}
        return {"workflow_runs": [{"id": 11, "head_sha": "head", "status": status, "conclusion": None}]}

    code, _, left = _replay_grade_job(monkeypatch, 21.0, reply)
    assert code == 0
    assert left >= 0


def _run(run_id: int, path: str, event: str, head: str = "head") -> dict[str, Any]:
    return {"id": run_id, "path": path, "event": event, "head_sha": head, "status": "in_progress", "conclusion": None}


def _answer(
    monkeypatch: pytest.MonkeyPatch, replies: dict[str, list[dict[str, Any]]]
) -> tuple[ModuleType, list[str], list[float]]:
    """Answer each endpoint with its queued replies in order, repeating the last one."""
    from scripts import ci_tests_start

    calls: list[str] = []
    sleeps: list[float] = []

    def api(endpoint: str) -> dict[str, Any]:
        calls.append(endpoint)
        queued = replies[endpoint]
        return queued.pop(0) if len(queued) > 1 else queued[0]

    monkeypatch.setattr(ci_tests_start, "_api", api)
    monkeypatch.setattr(ci_tests_start.time, "sleep", sleeps.append)
    return ci_tests_start, calls, sleeps


@pytest.mark.feature("gate-protected-finds-the-tests-run-through-check-runs")
def test_finds_the_tests_run_through_the_head_check_runs_while_the_listing_omits_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate run 44161874082 failed while the listing omitted Tests run 44161884281, whose check runs sat on the head."""
    gate = _run(44161874082, ".github/workflows/gate-protected.yml", "pull_request_target")
    tests = _run(44161884281, ".github/workflows/test.yml", "pull_request")
    jobs = "repos/owner/repo/actions/runs/44161884281/jobs?filter=latest&per_page=1"
    module, calls, sleeps = _answer(
        monkeypatch,
        {
            LISTING: [{"total_count": 0, "workflow_runs": []}],
            CHECKS: [
                {
                    "check_runs": [
                        {"details_url": LINK.format(44161874082, 134317548596)},
                        {"details_url": None},
                        {"details_url": "https://sonar.example/dashboard?id=agentihooks"},
                        {"details_url": LINK.format(44161884281, 134364720717)},
                        {"details_url": LINK.format(44161884281, 134364720609)},
                    ]
                }
            ],
            "repos/owner/repo/actions/runs/44161874082": [gate],
            "repos/owner/repo/actions/runs/44161884281": [tests],
            jobs: [{"total_count": 1}],
        },
    )
    assert module.verify("owner/repo", "head", waits=120) == ""
    assert calls == [LISTING, CHECKS, "repos/owner/repo/actions/runs/44161884281", jobs]
    assert sleeps == []


@pytest.mark.feature("gate-protected-finds-the-tests-run-through-check-runs")
def test_check_runs_of_other_runs_are_read_once_and_never_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    others = [
        _run(21, ".github/workflows/gate-protected.yml", "pull_request"),
        _run(22, ".github/workflows/test.yml", "push"),
        _run(23, ".github/workflows/test.yml", "pull_request", head="other"),
    ]
    module, calls, sleeps = _answer(
        monkeypatch,
        {
            LISTING: [{"workflow_runs": []}],
            CHECKS: [
                {
                    "check_runs": [{"details_url": LINK.format(run["id"], 9)} for run in others]
                    + [{"details_url": "https://github.com/owner/repo/actions/runs/24x/job/9"}]
                }
            ],
            **{f"repos/owner/repo/actions/runs/{run['id']}": [run] for run in others},
        },
    )
    assert module.verify("owner/repo", "head", attempts=2, interval=4) == "Tests run is missing for this head."
    assert calls == [
        LISTING,
        CHECKS,
        "repos/owner/repo/actions/runs/23",
        "repos/owner/repo/actions/runs/22",
        "repos/owner/repo/actions/runs/21",
        LISTING,
        CHECKS,
    ]
    assert sleeps == [4]


def test_a_tests_run_found_through_check_runs_on_a_later_poll_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = {"details_url": LINK.format(31, 1)}
    jobs = "repos/owner/repo/actions/runs/21/jobs?filter=latest&per_page=1"
    module, calls, sleeps = _answer(
        monkeypatch,
        {
            LISTING: [{"workflow_runs": []}],
            CHECKS: [{"check_runs": [gate]}, {"check_runs": [gate, {"details_url": LINK.format(21, 2)}]}],
            "repos/owner/repo/actions/runs/31": [_run(31, ".github/workflows/gate-protected.yml", "pull_request")],
            "repos/owner/repo/actions/runs/21": [_run(21, ".github/workflows/test.yml", "pull_request")],
            jobs: [{"total_count": 1}],
        },
    )
    assert module.verify("owner/repo", "head", interval=4) == ""
    assert calls == [
        LISTING,
        CHECKS,
        "repos/owner/repo/actions/runs/31",
        LISTING,
        CHECKS,
        "repos/owner/repo/actions/runs/21",
        jobs,
    ]
    assert sleeps == [4]


def test_the_check_runs_lookup_follows_the_requested_event(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = "repos/owner/repo/actions/workflows/test.yml/runs?head_sha=head&event=push&per_page=1"
    jobs = "repos/owner/repo/actions/runs/31/jobs?filter=latest&per_page=1"
    module, calls, sleeps = _answer(
        monkeypatch,
        {
            listing: [{"workflow_runs": []}],
            CHECKS: [{"check_runs": [{"details_url": LINK.format(31, 1)}, {"details_url": LINK.format(32, 2)}]}],
            "repos/owner/repo/actions/runs/31": [_run(31, ".github/workflows/test.yml", "push")],
            "repos/owner/repo/actions/runs/32": [_run(32, ".github/workflows/test.yml", "pull_request")],
            jobs: [{"total_count": 1}],
        },
    )
    assert module.verify("owner/repo", "head", event="push") == ""
    assert calls == [listing, CHECKS, "repos/owner/repo/actions/runs/32", "repos/owner/repo/actions/runs/31", jobs]
    assert sleeps == []


@pytest.mark.feature("gate-protected-finds-the-tests-run-through-check-runs")
def test_the_newest_tests_run_named_by_check_runs_is_read_first(monkeypatch: pytest.MonkeyPatch) -> None:
    newest = 44161884281
    cancelled = {"status": "completed", "conclusion": "cancelled"}
    jobs = f"repos/owner/repo/actions/runs/{newest}/jobs?filter=latest&per_page=1"
    module, calls, sleeps = _answer(
        monkeypatch,
        {
            LISTING: [{"workflow_runs": []}],
            CHECKS: [{"check_runs": [{"details_url": LINK.format(run, 1)} for run in (9, newest - 1, newest)]}],
            "repos/owner/repo/actions/runs/9": [{**_run(9, ".github/workflows/test.yml", "pull_request"), **cancelled}],
            f"repos/owner/repo/actions/runs/{newest - 1}": [
                {**_run(newest - 1, ".github/workflows/test.yml", "pull_request"), **cancelled}
            ],
            f"repos/owner/repo/actions/runs/{newest}": [_run(newest, ".github/workflows/test.yml", "pull_request")],
            jobs: [{"total_count": 1}],
        },
    )
    assert module.verify("owner/repo", "head") == ""
    assert calls == [LISTING, CHECKS, f"repos/owner/repo/actions/runs/{newest}", jobs]
    assert sleeps == []


@pytest.mark.feature("gate-protected-waits-for-a-late-created-tests-run")
@pytest.mark.parametrize(
    "start,created", [(17.0, 250.0), (37.0, 255.0), (16.0, 372.0)], ids=["44382072721", "44382391118", "44381971975"]
)
def test_the_grade_job_waits_for_a_tests_run_github_creates_late(
    monkeypatch: pytest.MonkeyPatch, start: float, created: float
) -> None:
    """Replayed 18 November gate runs polled from ``start`` s after the push; their Tests run came at ``created``."""

    def reply(endpoint: str, now: float) -> dict[str, Any]:
        if "/jobs?" in endpoint:
            return {"total_count": 1}
        runs = [{"id": 11, "head_sha": "head", "status": "queued", "conclusion": None}]
        return {"workflow_runs": runs if now >= created else [], "check_runs": []}

    code, _, left = _replay_grade_job(monkeypatch, start, reply, GITHUB_CALL_SECONDS)
    assert code == 0
    assert left >= 0


@pytest.mark.feature("gate-protected-waits-for-a-late-created-tests-run")
def test_the_grade_job_reports_a_tests_run_never_created_inside_its_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A head GitHub never creates a Tests run for is red after the grade job's 120 polls, before the job times out."""
    code, calls, left = _replay_grade_job(
        monkeypatch, 21.0, lambda endpoint, now: {"workflow_runs": [], "check_runs": []}, GITHUB_CALL_SECONDS
    )
    assert code == 1
    assert calls == [LISTING, CHECKS] * 120
    assert left >= 0
