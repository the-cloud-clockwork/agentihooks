import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts.swarm import cli, idle, ledger_events, mutation_wait, waits
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_waits import ME

pytestmark = pytest.mark.unit
URL = "https://github.com/org/repo/actions/runs/123"
RUN = "repos/org/repo/actions/runs/123"
PR = "https://github.com/org/repo/pull/7"
PULL = "repos/org/repo/pulls/7"
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
        assert kwargs.pop("text", False) == (args[2] in ("graphql", PULL))
        assert kwargs == {"capture_output": True, "timeout": 20}
        endpoint = args[2]
        state.asked.append(endpoint)
        if endpoint == "rate_limit":
            body = {"resources": {"core": state.core}}
            return completed(args, 0, json.dumps(body), "")
        if state.limited or (state.jobs_limited and "/jobs?" in endpoint):
            return completed(args, 1, "", LIMITED.decode())
        if "/jobs?" in endpoint:
            body = {"jobs": [{"name": "unit (3)", "conclusion": "failure"}]}
        elif "/git/ref/heads/" in endpoint:
            body = {"object": {"sha": "first"}}
        elif endpoint == PULL:
            body = {"head": {"sha": "first"}}
        else:
            body = state.run
        return completed(args, 0, json.dumps(body), "")

    def completed(args, code, out, err):
        if args[2] in ("graphql", PULL):
            return subprocess.CompletedProcess(args, code, out, err)
        return subprocess.CompletedProcess(args, code, out.encode(), err.encode())

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
    lines = (github.home / "github-backoff.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in lines] == [
        {"at": NOW, "endpoint": RUN, "until": RESET},
        {"at": NOW, "admitted_pending": URL},
    ]


def test_an_unreadable_first_pass_keeps_a_pending_admission_open(github, monkeypatch):
    github.limited = False
    monkeypatch.setattr(
        subprocess, "run", lambda args, **kwargs: subprocess.CompletedProcess(args, 1, b"", b"HTTP 502")
    )
    held = {**waits.on("mutation", URL), "head": None}
    assert waits.resolution(held, {}, None, None, None, False) == ""
    assert held["head"] is None


def test_an_unreadable_branch_head_on_the_first_pass_keeps_a_pending_admission_open(github, monkeypatch):
    github.limited = False
    real = subprocess.run

    def api(args, **kwargs):
        if "/git/ref/heads/" in args[2]:
            github.asked.append(args[2])
            return subprocess.CompletedProcess(args, 1, b"", b"HTTP 502")
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", api)
    held = {**waits.on("mutation", URL), "head": None}
    assert waits.resolution(held, {}, None, None, None, False) == ""
    assert held["head"] is None
    assert github.asked == [RUN, "repos/org/repo/git/ref/heads/feature/x"]


def test_an_unreadable_quota_endpoint_backs_off_one_minute(github, monkeypatch):
    real = subprocess.run

    def api(args, **kwargs):
        if args[2] == "rate_limit":
            github.asked.append("rate_limit")
            return subprocess.CompletedProcess(args, 1, b"", b"HTTP 502")
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", api)
    assert mutation_wait.run_resolution(URL) == ""
    assert mutation_wait._held_until() == NOW + 60


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
    assert mutation_wait.hold(RUN) == RESET
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


def test_a_rate_limited_pull_request_head_admits_the_checks_wait_pending(env, github, monkeypatch):  # noqa: F811
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    ledger.tasks = lambda slug: list(ledger.rows.values())
    monkeypatch.setattr(cli.ledger_events, "head", lambda url: ledger_events.head(url, subprocess.run))
    assert cli.main(["sw", "--as", ME, "wait", "--on", "checks", PR]) == 0
    assert idle.wait(store.redis, "sw", ME)["on"] == {"kind": "checks", "target": PR, "head": None}
    assert github.asked == [PULL, "rate_limit"]
    lines = (github.home / "github-backoff.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in lines] == [
        {"at": NOW, "endpoint": PULL, "until": RESET},
        {"at": NOW, "admitted_pending": PR},
    ]


def test_pull_request_reads_back_off_until_the_quota_reset(github):
    assert ledger_events.head(PR, subprocess.run) is None
    github.limited = False
    github.now = RESET - 1
    assert ledger_events.head(PR, subprocess.run) is None
    assert ledger_events.view(PR, subprocess.run) is None
    assert github.asked == [PULL, "rate_limit"]
    github.now = RESET + 1
    assert ledger_events.head(PR, subprocess.run) == "first"


def test_a_rate_limited_checks_view_holds_the_shared_backoff(github):
    assert ledger_events.view(PR, subprocess.run) is None
    assert github.asked == ["graphql", "rate_limit"]
    github.limited = False
    assert mutation_wait.run_resolution(URL) == ""
    assert github.asked == ["graphql", "rate_limit"]


def test_a_pending_checks_wait_binds_the_head_on_its_first_readable_view(github):
    pull = SimpleNamespace(state="OPEN", head="first")
    held = {**waits.on("checks", PR), "head": None}
    assert waits.checks_resolution(held, lambda target: pull) == ""
    assert held["head"] == "first"
