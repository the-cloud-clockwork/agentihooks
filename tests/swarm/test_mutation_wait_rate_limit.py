import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts.swarm import cli, idle, mutation_wait, waits
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_waits import ME

pytestmark = pytest.mark.unit
URL = "https://github.com/org/repo/actions/runs/123"
RUN = "repos/org/repo/actions/runs/123"
LIMITED = b"gh: API rate limit exceeded for user ID 31187725. (HTTP 403)"
NOW = 1_792_561_000.0
RESET = 1_792_561_590


@pytest.fixture
def github(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setattr(mutation_wait, "time", SimpleNamespace(sleep=lambda pause: None))
    state = SimpleNamespace(
        now=NOW,
        limited=True,
        core={"remaining": 0, "reset": RESET},
        run={
            "path": ".github/workflows/mutation-preflight.yml",
            "event": "push",
            "head_sha": "first",
            "head_branch": "feature/x",
            "status": "in_progress",
            "conclusion": None,
        },
        jobs_limited=False,
        asked=[],
        home=tmp_path,
    )
    monkeypatch.setattr(mutation_wait, "_now", lambda: state.now)

    def api(args, **kwargs):
        assert args[:2] == ["gh", "api"]
        assert kwargs == {"capture_output": True, "timeout": 20}
        endpoint = args[2]
        state.asked.append(endpoint)
        if endpoint == "rate_limit":
            body = {"resources": {"core": state.core}}
            return subprocess.CompletedProcess(args, 0, json.dumps(body).encode(), b"")
        if state.limited or (state.jobs_limited and "/jobs?" in endpoint):
            return subprocess.CompletedProcess(args, 1, b"", LIMITED)
        if "/jobs?" in endpoint:
            body = {"jobs": [{"name": "unit (3)", "conclusion": "failure"}]}
        elif "/git/ref/heads/" in endpoint:
            body = {"object": {"sha": "first"}}
        else:
            body = state.run
        return subprocess.CompletedProcess(args, 0, json.dumps(body).encode(), b"")

    monkeypatch.setattr(subprocess, "run", api)
    return state


def test_a_rate_limited_run_read_admits_the_mutation_wait_pending(env, github):  # noqa: F811
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    ledger.tasks = lambda slug: list(ledger.rows.values())
    assert cli.main(["sw", "--as", ME, "wait", "--on", "mutation", URL]) == 0
    held = idle.wait(store.redis, "sw", ME)["on"]
    assert held == {"kind": "mutation", "target": URL, "head": None}
    assert github.asked == [RUN, "rate_limit"]


def test_reads_back_off_until_the_core_quota_reset(github):
    held = {**waits.on("mutation", URL), "head": None}
    assert waits.resolution(held, {}, None, None, None, False) == ""
    assert github.asked == [RUN, "rate_limit"]
    github.limited = False
    github.now = RESET - 1
    assert waits.resolution(held, {}, None, None, None, False) == ""
    assert mutation_wait.run_resolution(URL) == ""
    assert github.asked == [RUN, "rate_limit"]
    github.now = RESET + 1
    assert waits.resolution(held, {}, None, None, None, False) == ""
    assert held["head"] == "first"
    assert github.asked[2:] == [RUN, "repos/org/repo/git/ref/heads/feature/x", RUN]


def test_a_secondary_limit_backs_off_one_minute_while_core_reports_quota(github):
    github.core = {"remaining": 4000, "reset": RESET}
    assert mutation_wait.run_resolution(URL) == ""
    github.limited = False
    github.now = NOW + 59
    assert mutation_wait.run_resolution(URL) == ""
    assert github.asked == [RUN, "rate_limit"]
    github.now = NOW + 61
    assert mutation_wait.run_resolution(URL) == ""
    assert github.asked == [RUN, "rate_limit", RUN]


def test_a_secondary_limit_never_shortens_a_longer_core_backoff(github):
    mutation_wait.run_resolution(URL)
    github.core = {"remaining": 4000, "reset": RESET}
    assert mutation_wait._hold(RUN) == RESET
    github.limited = False
    github.now = NOW + 61
    assert mutation_wait.run_resolution(URL) == ""
    assert github.asked == [RUN, "rate_limit", "rate_limit"]


def test_an_unwritable_backoff_home_still_admits_and_holds_pending(github, monkeypatch):
    blocked = github.home / "blocked"
    blocked.write_text("")
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(blocked))
    assert mutation_wait.bind(URL) is None
    held = {**waits.on("mutation", URL), "head": None}
    assert waits.resolution(held, {}, None, None, None, False) == ""


def test_every_rate_limited_read_is_logged_with_its_backoff(github):
    mutation_wait.run_resolution(URL)
    lines = (github.home / "github-backoff.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in lines] == [{"at": NOW, "endpoint": RUN, "until": RESET}]


def test_a_pending_admission_ends_red_once_the_run_reads_as_unrelated(github):
    github.limited = False
    github.run["path"] = "tests.yml"
    held = {**waits.on("mutation", URL), "head": None}
    assert waits.resolution(held, {}, None, None, None, False) == (
        f"mutation preflight {URL}, now red; "
        "wait on mutation needs a readable branch Mutation preflight or focused mutation proof run url"
    )


def test_a_rate_limited_branch_head_read_keeps_the_wait(github, monkeypatch):
    github.limited = False
    github.run.update(status="completed", conclusion="success")
    real = subprocess.run

    def api(args, **kwargs):
        if "/git/ref/heads/" in args[2]:
            github.asked.append(args[2])
            return subprocess.CompletedProcess(args, 1, b"", LIMITED)
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", api)
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == ""
    assert github.asked == [RUN, "repos/org/repo/git/ref/heads/feature/x", "rate_limit"]


def test_a_rate_limited_jobs_read_keeps_a_red_run_wait_for_its_evidence(github):
    github.limited = False
    github.jobs_limited = True
    github.run.update(status="completed", conclusion="failure")
    assert mutation_wait.run_resolution(URL) == ""
    github.jobs_limited = False
    github.now = RESET + 1
    assert mutation_wait.run_resolution(URL) == (
        f"checks on {URL}, now red; the run concluded failure; failed jobs: unit (3) failure"
    )
