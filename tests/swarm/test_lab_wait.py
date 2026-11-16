import json
import subprocess

import pytest

from scripts.swarm import github_reads, idle, waits
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_waits import ME, held, started, tick  # noqa: F401

pytestmark = pytest.mark.unit

CHECK = "https://github.com/org/repo/runs/456"
PULL_CHECK = "https://github.com/org/repo/pull/7/checks?check_run_id=456"
ENDPOINT = "repos/org/repo/check-runs/456"


@pytest.fixture
def github(monkeypatch):
    state = {
        "check": {"name": "lab/tests", "status": "in_progress", "conclusion": None},
        "code": 0,
        "stderr": b"",
        "asked": [],
        "callers": [],
    }

    def api(args, **kwargs):
        state["asked"].append(args)
        state["callers"].append(github_reads.CALLER.get(None))
        out = json.dumps(state["check"]).encode() if state["code"] == 0 else b""
        return subprocess.CompletedProcess(args, state["code"], out, state["stderr"])

    monkeypatch.setattr(subprocess, "run", api)
    return state


def hold_check(rig, target=CHECK):
    idle.declare_wait(rig.store.redis, "sw", ME, 10_000_000, "", 1, on=waits.on("lab", target))


def notice(outcome):
    return (
        f"Your wait on {outcome} has ended. Pick task t1 back up: agentihooks swarm sw done, block, "
        "or wait on the next thing."
    )


@pytest.mark.parametrize("target", [CHECK, PULL_CHECK])
def test_a_lab_wait_takes_a_check_run_url(target):
    assert waits.target_problem("lab", target, "t1", {}, None) == ""


@pytest.mark.parametrize(
    "target",
    [
        "https://github.com/org/repo/actions/runs/456",
        "https://github.com/org/repo/pull/7",
        "https://github.com/org/repo/runs/456/logs",
        "http://github.com/org/repo/runs/456",
        "https://lab.example/grades/456",
    ],
)
@pytest.mark.feature("swarm-lab-wait")
def test_a_lab_wait_refuses_anything_but_a_check_run_url(target):
    assert waits.target_problem("lab", target, "t1", {}, None) == (
        f"wait on lab needs a lab check run url, not {target}"
    )


def test_the_cli_records_a_lab_wait_on_a_check_run(started, capsys):  # noqa: F811
    store, _ = started
    assert run("sw", "--as", ME, "wait", "--on", "lab", CHECK, "--reason", "lab grade") == 0
    assert held(store)["on"] == {"kind": "lab", "target": CHECK}
    assert json.loads(capsys.readouterr().out)["on"] == {"kind": "lab", "target": CHECK}


@pytest.mark.parametrize("status", ["queued", "in_progress"])
@pytest.mark.feature("swarm-lab-wait")
def test_a_lab_check_still_running_holds_the_wait(tick, github, status):  # noqa: F811
    github["check"] = {"name": "lab/tests", "status": status, "conclusion": None}
    hold_check(tick)
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"] == {"kind": "lab", "target": CHECK}
    assert github["asked"] == [["gh", "api", ENDPOINT]]
    assert github["callers"] == ["waits.lab"]
    assert tick.told() == []


@pytest.mark.parametrize("target", [CHECK, PULL_CHECK])
@pytest.mark.feature("swarm-lab-wait")
def test_the_tick_ends_a_green_lab_wait_with_the_inbox_notice(tick, github, target):  # noqa: F811
    github["check"] = {"name": "lab/tests", "status": "completed", "conclusion": "success"}
    hold_check(tick, target)
    outcome = f"lab check {target}, now green"
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert github["asked"] == [["gh", "api", ENDPOINT]]
    assert tick.told() == [notice(outcome)]


@pytest.mark.parametrize(
    "name, conclusion, gate",
    [
        ("lab/tests", "failure", "tests"),
        ("lab-shadow/mutation", "timed_out", "mutation"),
        ("lab/unit/shard-1", "cancelled", "unit/shard-1"),
        ("Lint", "action_required", "Lint"),
    ],
)
@pytest.mark.feature("swarm-lab-wait")
def test_the_tick_ends_a_red_lab_wait_naming_the_gate_and_conclusion(tick, github, name, conclusion, gate):  # noqa: F811
    github["check"] = {"name": name, "status": "completed", "conclusion": conclusion}
    hold_check(tick)
    outcome = f"lab check {CHECK}, now red; gate {gate} concluded {conclusion}"
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert tick.told() == [notice(outcome)]


def test_an_unreachable_github_holds_the_lab_wait(tick, github):  # noqa: F811
    github["code"], github["stderr"] = 1, b"HTTP 502: Bad Gateway"
    hold_check(tick)
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"]["target"] == CHECK


def test_a_missing_check_run_ends_the_lab_wait_with_that_reason(tick, github):  # noqa: F811
    github["code"], github["stderr"] = 1, b"gh: Not Found (HTTP 404)"
    hold_check(tick)
    outcome = f"lab check {CHECK}, missing; GitHub has no such check run"
    assert waits.lab_wait.resolution(CHECK) == outcome
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]


def test_the_kinds_list_names_the_lab_wait():
    assert waits.KINDS == ("checks", "merge", "reply", "task", "intent", "lab")
