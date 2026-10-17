import json
import subprocess

import pytest

from scripts.swarm import cli, idle, mutation_wait, waits
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_waits import ME, held, started, tick  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")

RUN = "https://github.com/org/repo/actions/runs/123"
ENDPOINT = "repos/org/repo/actions/runs/123"
JOBS = f"{ENDPOINT}/jobs?per_page=100"


@pytest.fixture
def actions(monkeypatch):
    state = {
        "run": {"status": "in_progress", "conclusion": None},
        "jobs": {"jobs": []},
        "jobs_code": 0,
        "code": 0,
        "stderr": b"",
        "asked": [],
    }

    def api(args, **kwargs):
        state["asked"].append(args)
        assert kwargs == {"capture_output": True, "timeout": 20}
        if args == ["gh", "api", JOBS]:
            out = json.dumps(state["jobs"]).encode() if state["jobs_code"] == 0 else b""
            return subprocess.CompletedProcess(args, state["jobs_code"], out, b"HTTP 502: Bad Gateway")
        out = json.dumps(state["run"]).encode() if state["code"] == 0 else b""
        return subprocess.CompletedProcess(args, state["code"], out, state["stderr"])

    monkeypatch.setattr(subprocess, "run", api)
    return state


def hold_run(rig):
    idle.declare_wait(rig.store.redis, "sw", ME, 10_000_000, "", 1, on=waits.on("checks", RUN))


def test_a_checks_wait_accepts_an_actions_run_without_reading_a_pull_request_head(
    started,  # noqa: F811
    monkeypatch,
    capsys,
):
    store, _ = started
    monkeypatch.setattr(cli.ledger_events, "head", lambda url: pytest.fail("a pull request head was read"))
    assert run("sw", "--as", ME, "wait", "--on", "checks", RUN, "--reason", "proof run") == 0
    assert held(store)["on"] == {"kind": "checks", "target": RUN}
    assert json.loads(capsys.readouterr().out)["on"] == {"kind": "checks", "target": RUN}


def test_checks_take_a_pull_request_or_a_run_and_merge_takes_only_a_pull_request():
    assert waits.target_problem("checks", RUN, "t1", {}, None) == ""
    assert waits.target_problem("checks", RUN + "/jobs/7", "t1", {}, None) == (
        f"wait on checks needs a pull request url or an Actions run url, not {RUN}/jobs/7"
    )
    assert waits.target_problem("merge", RUN, "t1", {}, None) == f"wait on merge needs a pull request url, not {RUN}"


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting"])
def test_a_run_still_going_keeps_the_wait(tick, actions, status):  # noqa: F811
    actions["run"] = {"status": status, "conclusion": None}
    hold_run(tick)
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"]["target"] == RUN
    assert actions["asked"] == [["gh", "api", ENDPOINT]]


@pytest.mark.parametrize(
    "conclusion, outcome",
    [
        ("success", f"checks on {RUN}, now green"),
        ("failure", f"checks on {RUN}, now red; the run concluded failure"),
        ("timed_out", f"checks on {RUN}, now red; the run concluded timed_out"),
        ("cancelled", f"checks on {RUN}, cancelled; dispatch the run again and wait on the new one"),
    ],
)
def test_the_tick_ends_a_run_wait_with_its_conclusion(tick, actions, conclusion, outcome):  # noqa: F811
    actions["run"] = {"status": "completed", "conclusion": conclusion}
    hold_run(tick)
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert tick.told() == [
        f"Your wait on {outcome} has ended. Pick task t1 back up: agentihooks swarm sw done, block, "
        "or wait on the next thing."
    ]


def test_a_missing_run_ends_the_wait_with_that_reason(tick, actions):  # noqa: F811
    actions["code"], actions["stderr"] = 1, b"gh: Not Found (HTTP 404)"
    hold_run(tick)
    assert mutation_wait.run_resolution(RUN) == f"checks on {RUN}, missing; GitHub has no such run"
    assert tick.end() == [f"ended the wait of {ME}: checks on {RUN}, missing; GitHub has no such run"]


def test_an_unreachable_github_keeps_the_run_wait(tick, actions):  # noqa: F811
    actions["code"], actions["stderr"] = 1, b"HTTP 502: Bad Gateway"
    hold_run(tick)
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"]["target"] == RUN


def job(name, conclusion):
    return {"name": name, "status": "completed", "conclusion": conclusion}


def test_a_red_run_names_its_failed_jobs_and_ends_the_wait(tick, actions):  # noqa: F811
    actions["run"] = {"status": "completed", "conclusion": "failure"}
    actions["jobs"] = {
        "jobs": [
            job("lint", "success"),
            job("unit (3.11)", "failure"),
            job("unit (3.12)", "timed_out"),
            job("mutation", "cancelled"),
            job("docs", "skipped"),
        ]
    }
    hold_run(tick)
    outcome = (
        f"checks on {RUN}, now red; the run concluded failure; "
        "failed jobs: unit (3.11) failure, unit (3.12) timed_out, mutation cancelled"
    )
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert tick.told() == [
        f"Your wait on {outcome} has ended. Pick task t1 back up: agentihooks swarm sw done, block, "
        "or wait on the next thing."
    ]
    assert actions["asked"] == [["gh", "api", ENDPOINT], ["gh", "api", JOBS]]


def test_a_green_run_reads_no_jobs(tick, actions):  # noqa: F811
    actions["run"] = {"status": "completed", "conclusion": "success"}
    hold_run(tick)
    assert tick.end() == [f"ended the wait of {ME}: checks on {RUN}, now green"]
    assert actions["asked"] == [["gh", "api", ENDPOINT]]


def test_a_red_run_with_no_failed_job_names_only_its_conclusion(tick, actions):  # noqa: F811
    actions["run"] = {"status": "completed", "conclusion": "failure"}
    actions["jobs"] = {"jobs": [job("lint", "success"), job("docs", "skipped")]}
    hold_run(tick)
    assert tick.end() == [f"ended the wait of {ME}: checks on {RUN}, now red; the run concluded failure"]
    assert actions["asked"] == [["gh", "api", ENDPOINT], ["gh", "api", JOBS]]


def test_unreadable_jobs_still_end_a_red_run_wait_with_its_conclusion(tick, actions):  # noqa: F811
    actions["run"] = {"status": "completed", "conclusion": "failure"}
    actions["jobs_code"] = 1
    hold_run(tick)
    outcome = f"checks on {RUN}, now red; the run concluded failure; its jobs could not be read"
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None


@pytest.mark.parametrize(
    "answer",
    [{"message": "no jobs"}, [], {"jobs": ["unit"]}, {"jobs": [{"conclusion": "failure"}]}],
    ids=["no jobs key", "not an object", "job not an object", "job without a name"],
)
def test_a_malformed_jobs_answer_still_ends_a_red_run_wait(tick, actions, answer):  # noqa: F811
    actions["run"] = {"status": "completed", "conclusion": "timed_out"}
    actions["jobs"] = answer
    hold_run(tick)
    outcome = f"checks on {RUN}, now red; the run concluded timed_out; its jobs could not be read"
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None
