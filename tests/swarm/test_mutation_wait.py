import io
import json
import subprocess
import zipfile
from types import SimpleNamespace

import pytest

from scripts.swarm import cli, idle, waits
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_waits import ME, tick  # noqa: F401

pytestmark = pytest.mark.unit
URL = "https://github.com/org/repo/actions/runs/123"


def test_cli_wait_binds_the_branch_preflight_run(env, monkeypatch):  # noqa: F811
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    ledger.tasks = lambda slug: list(ledger.rows.values())
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            json.dumps(
                {
                    "path": ".github/workflows/mutation-preflight.yml",
                    "event": "push",
                    "head_sha": "first",
                    "status": "in_progress",
                }
            ),
        ),
    )
    assert cli.main(["sw", "--as", ME, "wait", "--on", "mutation", URL]) == 0
    assert idle.wait(store.redis, "sw", ME)["on"] == {
        "kind": "mutation",
        "target": URL,
        "head": "first",
    }
    assert waits.resolution({"kind": "mutation", "target": URL, "head": "first"}, {}, None, None, None, False) == ""


@pytest.fixture
def preflight(monkeypatch):
    report_at = "2026-10-09T10:01:00Z"
    state = SimpleNamespace(
        run={
            "path": ".github/workflows/mutation-preflight.yml",
            "event": "push",
            "head_sha": "first",
            "run_started_at": "2026-10-09T10:00:00Z",
            "status": "completed",
            "conclusion": "success",
        },
        report={"files": [], "not_mutated": [], "failed": False},
        missing=False,
        report_name="report.json",
        artifacts=[
            {"id": 7, "name": "mutation-preflight-report", "expired": False, "created_at": report_at},
            {"id": 6, "name": "mutation-preflight-report", "expired": False, "created_at": report_at},
            {"id": 9, "name": "other-report", "expired": False, "created_at": report_at},
            {"id": 8, "name": "mutation-preflight-report", "expired": True, "created_at": report_at},
        ],
        requests=[],
    )

    def api(args, **kwargs):
        assert args[:2] == ["gh", "api"]
        assert 0 < kwargs.get("timeout", float("inf")) <= 20
        state.requests.append(args[-1])
        if args[-1].endswith("/zip"):
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as zipped:
                zipped.writestr(state.report_name, json.dumps(state.report))
            output = data.getvalue()
        elif "artifacts?" in args[-1]:
            output = json.dumps({"artifacts": [] if state.missing else state.artifacts})
        else:
            output = json.dumps(state.run)
        if kwargs.get("text", False):
            output = output.decode() if isinstance(output, bytes) else output
        else:
            output = output.encode() if isinstance(output, str) else output
        return subprocess.CompletedProcess(args, 0, output if kwargs.get("capture_output") else None)

    monkeypatch.setattr(subprocess, "run", api)
    return state


def test_the_tick_ends_a_clean_preflight_once(tick, preflight):  # noqa: F811
    held = {**waits.on("mutation", URL), "head": "first"}
    idle.declare_wait(tick.store.redis, "sw", ME, 10_000_000, "", 1, on=held)
    assert tick.end() == [f"ended the wait of {ME}: mutation preflight {URL}, now green; no failing mutants"]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert len(tick.told()) == 1
    assert tick.end() == []
    assert preflight.requests == [
        "repos/org/repo/actions/runs/123",
        "repos/org/repo/actions/runs/123/artifacts?per_page=100",
        "repos/org/repo/actions/artifacts/7/zip",
    ]


def test_the_tick_names_every_failing_mutant_and_unmutated_file(tick, preflight):  # noqa: F811
    preflight.run["conclusion"] = "failure"
    preflight.report = {
        "files": [
            {
                "path": "scripts/swarm/waits.py",
                "failures": [
                    {"name": "mutant_one", "status": "survived", "fingerprint": "aaa"},
                    {"name": "mutant_two", "status": "no tests", "fingerprint": "bbb"},
                ],
            }
        ],
        "not_mutated": [{"path": "scripts/swarm/cli.py", "reason": "budget exhausted"}],
        "failed": True,
    }
    held = {**waits.on("mutation", URL), "head": "first"}
    idle.declare_wait(tick.store.redis, "sw", ME, 10_000_000, "", 1, on=held)
    tick.end()
    assert "now red" in tick.told()[0]
    assert "scripts/swarm/waits.py:mutant_one:aaa (survived)" in tick.told()[0]
    assert "scripts/swarm/waits.py:mutant_two:bbb (no tests)" in tick.told()[0]
    assert "scripts/swarm/cli.py: budget exhausted" in tick.told()[0]
    assert idle.wait(tick.store.redis, "sw", ME) is None


@pytest.mark.parametrize("status", ["queued", "in_progress"])
def test_an_active_preflight_keeps_the_wait(tick, preflight, status):  # noqa: F811
    preflight.run["status"] = status
    held = {**waits.on("mutation", URL), "head": "first"}
    idle.declare_wait(tick.store.redis, "sw", ME, 10_000_000, "", 1, on=held)
    assert tick.end() == []
    assert idle.wait(tick.store.redis, "sw", ME)["on"] == held
    assert tick.told() == []
    assert len(preflight.requests) == 1


def test_a_successful_run_without_a_mutation_report_ends_red(preflight):
    preflight.missing = True
    held = {**waits.on("mutation", URL), "head": "first"}
    assert "now red; complete mutation report unavailable" in waits.resolution(held, {}, None, None, None, False)


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "skipped"])
def test_an_incomplete_run_ends_red_without_claiming_a_pass(preflight, conclusion):
    preflight.run["conclusion"] = conclusion
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == (
        f"mutation preflight {URL}, now red; run {conclusion}"
    )
    assert len(preflight.requests) == 1


def test_an_unreadable_run_keeps_the_wait(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, ""))
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == ""


@pytest.mark.parametrize("field,value", [("path", "tests.yml"), ("event", "pull_request"), ("head_sha", "other")])
def test_a_run_that_changes_its_identity_ends_red(preflight, field, value):
    preflight.run[field] = value
    held = {**waits.on("mutation", URL), "head": "first"}
    assert "run no longer matches" in waits.resolution(held, {}, None, None, None, False)


@pytest.mark.parametrize("target", ["123", "https://github.com/org/repo/pull/1", URL + "/jobs/7"])
def test_wait_declaration_rejects_an_invalid_run_target(target):
    assert waits.target_problem("mutation", target, "t1", {}, None) == ("wait on mutation needs an Actions run url")


@pytest.mark.parametrize(
    "field,value",
    [("path", "tests.yml"), ("event", "pull_request"), ("head_sha", "")],
)
def test_cli_refuses_an_unrelated_or_unbound_run(env, preflight, field, value, capsys):  # noqa: F811
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    ledger.tasks = lambda slug: list(ledger.rows.values())
    preflight.run[field] = value
    assert cli.main(["sw", "--as", ME, "wait", "--on", "mutation", URL]) == 1
    assert "mutation" in capsys.readouterr().err
    assert idle.wait(store.redis, "sw", ME) is None


@pytest.mark.parametrize("conclusion,failed", [("failure", False), ("success", True), ("failure", True)])
def test_a_failed_run_or_report_cannot_pass_without_named_survivors(preflight, conclusion, failed):
    preflight.run["conclusion"] = conclusion
    preflight.report["failed"] = failed
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == (
        f"mutation preflight {URL}, now red; mutation failed without a named survivor"
    )


def test_report_read_failure_ends_red(preflight, monkeypatch):
    original = subprocess.run

    def api(args, **kwargs):
        if args[-1].endswith("/zip"):
            return subprocess.CompletedProcess(args, 0, b"unreadable")
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", api)
    held = {**waits.on("mutation", URL), "head": "first"}
    assert "complete mutation report unavailable" in waits.resolution(held, {}, None, None, None, False)


def test_a_nested_complete_report_can_pass(preflight):
    preflight.report_name = ".mutation-gate/report.json"
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == (
        f"mutation preflight {URL}, now green; no failing mutants"
    )


@pytest.mark.parametrize("report", [None, {}])
def test_a_malformed_report_cannot_pass(preflight, report):
    preflight.report = report
    held = {**waits.on("mutation", URL), "head": "first"}
    assert "complete mutation report unavailable" in waits.resolution(held, {}, None, None, None, False)


def test_an_archive_without_the_report_cannot_pass(preflight):
    preflight.report_name = "other.json"
    held = {**waits.on("mutation", URL), "head": "first"}
    assert "complete mutation report unavailable" in waits.resolution(held, {}, None, None, None, False)


def test_a_skipped_rerun_cannot_reuse_the_previous_attempts_report(preflight):
    preflight.run["run_started_at"] = "2026-10-09T10:02:00Z"
    held = {**waits.on("mutation", URL), "head": "first"}
    assert "complete mutation report unavailable" in waits.resolution(held, {}, None, None, None, False)
