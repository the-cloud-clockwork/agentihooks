import json
import subprocess
from typing import Any

import pytest

from scripts.swarm import github_reads, idle, lab_wait, waits
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_waits import ME, held, started, tick  # noqa: F401

pytestmark = pytest.mark.unit

CHECK = "https://github.com/org/repo/runs/456"
PULL_CHECK = "https://github.com/org/repo/pull/7/checks?check_run_id=456"
ENDPOINT = "repos/org/repo/check-runs/456"
SHA = "a" * 40
GRADE = f"http://lab.ci:8800/grades/{SHA}"


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "check": {"name": "lab/tests", "status": "in_progress", "conclusion": None},
        "code": 0,
        "stderr": b"",
        "asked": [],
        "callers": [],
    }

    def api(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        state["asked"].append(args)
        state["callers"].append(github_reads.CALLER.get(None))
        out = json.dumps(state["check"]).encode() if state["code"] == 0 else b""
        return subprocess.CompletedProcess(args, state["code"], out, state["stderr"])

    monkeypatch.setattr(subprocess, "run", api)
    return state


def hold_check(rig: Any, target: str = CHECK) -> None:
    idle.declare_wait(rig.store.redis, "sw", ME, 10_000_000, "", 1, on=waits.on("lab", target))


def notice(outcome: str) -> str:
    return (
        f"Your wait on {outcome} has ended. Pick task t1 back up: agentihooks swarm sw done, block, "
        "or wait on the next thing."
    )


@pytest.mark.parametrize("target", [CHECK, PULL_CHECK])
def test_a_lab_wait_takes_a_check_run_url(target: str) -> None:
    assert waits.target_problem("lab", target, "t1", {}, None) == ""


@pytest.mark.parametrize(
    "target",
    [
        "https://github.com/org/repo/actions/runs/456",
        "https://github.com/org/repo/pull/7",
        "https://github.com/org/repo/runs/456/logs",
        "https://github.com/org/repo/runs/",
        "https://github.com/org/repo/pull/7/checks?check_run_id=",
        "https://github.com/org/repo/pull/7/files?check_run_id=456",
        "http://github.com/org/repo/runs/456",
        "https://lab.example/grades/456",
        f"{GRADE}/runs",
        f"ftp://lab.ci/grades/{SHA}",
        f"http://lab.ci/grades/{SHA[:39]}",
        f"http://lab.ci/grades/{SHA}?head=1",
    ],
)
@pytest.mark.feature("swarm-lab-wait")
def test_a_lab_wait_refuses_anything_but_a_check_run_url(target: str) -> None:
    refusal = f"wait on lab needs a check run url or a lab grade url, not {target}"
    assert waits.target_problem("lab", target, "t1", {}, None) == refusal


@pytest.mark.feature("swarm-lab-wait")
def test_the_cli_records_a_lab_wait_on_a_check_run(
    started: tuple[Any, Any],  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, _ = started
    assert run("sw", "--as", ME, "wait", "--on", "lab", CHECK, "--reason", "lab grade") == 0
    assert (held(store) or {})["on"] == {"kind": "lab", "target": CHECK}
    assert json.loads(capsys.readouterr().out)["on"] == {"kind": "lab", "target": CHECK}


@pytest.mark.parametrize("status", ["queued", "in_progress"])
@pytest.mark.feature("swarm-lab-wait")
def test_a_lab_check_still_running_holds_the_wait(
    tick: Any,  # noqa: F811
    github: dict[str, Any],
    status: str,
) -> None:
    github["check"] = {"name": "lab/tests", "status": status, "conclusion": None}
    hold_check(tick)
    assert tick.end() == []
    assert (idle.wait(tick.store.redis, "sw", ME) or {})["on"] == {"kind": "lab", "target": CHECK}
    assert github["asked"] == [["gh", "api", ENDPOINT]]
    assert github["callers"] == ["waits.lab"]
    assert tick.told() == []


@pytest.mark.parametrize("target", [CHECK, PULL_CHECK])
@pytest.mark.feature("swarm-lab-wait")
def test_the_tick_ends_a_green_lab_wait_with_the_inbox_notice(
    tick: Any,  # noqa: F811
    github: dict[str, Any],
    target: str,
) -> None:
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
        ("lab/docs", "skipped", "docs"),
        ("lab/docs", "neutral", "docs"),
        ("Lint", "action_required", "Lint"),
    ],
)
@pytest.mark.feature("swarm-lab-wait")
def test_the_tick_ends_a_red_lab_wait_naming_the_gate_and_conclusion(
    tick: Any,  # noqa: F811
    github: dict[str, Any],
    name: str,
    conclusion: str,
    gate: str,
) -> None:
    github["check"] = {"name": name, "status": "completed", "conclusion": conclusion}
    hold_check(tick)
    outcome = f"lab check {CHECK}, now red; gate {gate} concluded {conclusion}"
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert tick.told() == [notice(outcome)]


@pytest.mark.feature("swarm-lab-wait")
def test_an_unreachable_github_holds_the_lab_wait(tick: Any, github: dict[str, Any]) -> None:  # noqa: F811
    github["code"], github["stderr"] = 1, b"HTTP 502: Bad Gateway"
    hold_check(tick)
    assert tick.end() == []
    assert (idle.wait(tick.store.redis, "sw", ME) or {})["on"]["target"] == CHECK


@pytest.mark.feature("swarm-lab-wait")
def test_a_missing_check_run_ends_the_lab_wait_with_that_reason(tick: Any, github: dict[str, Any]) -> None:  # noqa: F811
    github["code"], github["stderr"] = 1, b"gh: Not Found (HTTP 404)"
    hold_check(tick)
    outcome = f"lab check {CHECK}, missing; GitHub has no such check run"
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]


@pytest.mark.feature("swarm-lab-wait")
def test_a_stored_lab_wait_on_a_bad_target_ends_without_reading_github(
    tick: Any,  # noqa: F811
    github: dict[str, Any],
) -> None:
    target = "https://github.com/org/repo/runs/456/logs"
    hold_check(tick, target)
    outcome = f"lab check {target}, not a check run url; wait on a check run url"
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]
    assert github["asked"] == []


def test_the_kinds_list_names_the_lab_wait() -> None:
    assert waits.KINDS == ("checks", "merge", "reply", "task", "intent", "lab")


@pytest.fixture
def lab(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"grades": [], "error": None, "asked": []}

    def grades(url: str) -> list[dict[str, Any]]:
        state["asked"].append(url)
        if state["error"] is not None:
            raise state["error"]
        return state["grades"]

    monkeypatch.setattr(lab_wait, "grades", grades)
    return state


def grade(conclusion: str | None, checks: dict[str, str] | None = None, superseded: bool = False) -> dict[str, Any]:
    named = {name: {"status": "completed", "conclusion": end} for name, end in (checks or {}).items()}
    return {"run": "r1", "conclusion": conclusion, "superseded": superseded, "checks": named}


def hold_grade(rig: Any) -> None:
    idle.declare_wait(rig.store.redis, "sw", ME, 10_000_000, "", 1, on=waits.on("lab", GRADE))


@pytest.mark.parametrize("target", [GRADE, f"https://lab.example/ci/grades/{'b' * 64}"])
@pytest.mark.feature("swarm-lab-wait")
def test_a_lab_wait_takes_a_lab_grade_url(target: str) -> None:
    assert waits.target_problem("lab", target, "t1", {}, None) == ""


@pytest.mark.parametrize("found", [[], [grade(None)], [grade("success"), grade(None)]])
@pytest.mark.feature("swarm-lab-wait")
def test_a_lab_grade_not_yet_closed_holds_the_wait(
    tick: Any,  # noqa: F811
    lab: dict[str, Any],
    found: list[dict[str, Any]],
) -> None:
    lab["grades"] = found
    hold_grade(tick)
    assert tick.end() == []
    assert (idle.wait(tick.store.redis, "sw", ME) or {})["on"] == {"kind": "lab", "target": GRADE}
    assert lab["asked"] == [GRADE]
    assert tick.told() == []


@pytest.mark.parametrize("error", [OSError("refused"), ValueError("lab grades document carries no grades")])
@pytest.mark.feature("swarm-lab-wait")
def test_an_unreachable_lab_holds_the_grade_wait(
    tick: Any,  # noqa: F811
    lab: dict[str, Any],
    error: Exception,
) -> None:
    lab["error"] = error
    hold_grade(tick)
    assert tick.end() == []
    assert (idle.wait(tick.store.redis, "sw", ME) or {})["on"]["target"] == GRADE


@pytest.mark.feature("swarm-lab-wait")
def test_the_tick_ends_a_green_lab_grade_wait_with_the_inbox_notice(
    tick: Any,  # noqa: F811
    lab: dict[str, Any],
) -> None:
    lab["grades"] = [grade("success", {"Gate — Required": "success"}), grade(None, superseded=True)]
    hold_grade(tick)
    outcome = f"lab grade {GRADE}, now green"
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert tick.told() == [notice(outcome)]


@pytest.mark.parametrize(
    "found, failed",
    [
        ([grade("failure", {"lab/tests": "failure", "lab/lint": "success", "lab/docs": "skipped"})], "lab/tests"),
        (
            [grade("failure", {"lab/unit": "timed_out", "lab/types": "neutral", "lab/tests": "failure"})],
            "lab/tests, lab/unit",
        ),
        ([grade("success"), grade("failure", {"lab/tests": "cancelled"})], "lab/tests"),
        ([grade("failure")], "with no failed check named"),
    ],
)
@pytest.mark.feature("swarm-lab-wait")
def test_the_tick_ends_a_red_lab_grade_wait_naming_the_failed_checks(
    tick: Any,  # noqa: F811
    lab: dict[str, Any],
    found: list[dict[str, Any]],
    failed: str,
) -> None:
    lab["grades"] = found
    hold_grade(tick)
    outcome = f"lab grade {GRADE}, now red; failed {failed}"
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert tick.told() == [notice(outcome)]


@pytest.mark.feature("swarm-lab-wait")
def test_a_lab_grade_whose_every_grade_a_newer_head_replaced_ends_the_wait(
    tick: Any,  # noqa: F811
    lab: dict[str, Any],
) -> None:
    lab["grades"] = [grade(None, superseded=True), grade("failure", superseded=True)]
    hold_grade(tick)
    outcome = f"lab grade {GRADE}, superseded; a newer head replaced it"
    assert tick.end() == [f"ended the wait of {ME}: {outcome}"]
    assert tick.told() == [notice(outcome)]


class Reply:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "Reply":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_the_grade_read_asks_the_grade_url_within_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, float]] = []

    def urlopen(url: str, timeout: float) -> Reply:
        seen.append((url, timeout))
        return Reply(json.dumps({"head": SHA, "grades": [grade("success")]}).encode())

    monkeypatch.setattr(lab_wait, "urlopen", urlopen)
    assert lab_wait.grades(GRADE) == [grade("success")]
    assert seen == [(GRADE, lab_wait.TIMEOUT_S)]


@pytest.mark.parametrize("body", [b"[]", b'{"head": "x"}', b'{"grades": {}}'])
def test_a_grade_document_without_a_grades_list_is_refused(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    monkeypatch.setattr(lab_wait, "urlopen", lambda url, timeout: Reply(body))
    with pytest.raises(ValueError, match="^lab grades document carries no grades$"):
        lab_wait.grades(GRADE)
