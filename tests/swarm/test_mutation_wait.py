import io
import json
import subprocess
import zipfile
from types import SimpleNamespace

import pytest

from scripts.swarm import cli, idle, mutation_wait, waits
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_waits import ME, tick  # noqa: F401

pytestmark = pytest.mark.unit
URL = "https://github.com/org/repo/actions/runs/123"


@pytest.fixture(autouse=True)
def pauses(monkeypatch):
    slept = []
    monkeypatch.setattr(mutation_wait, "time", SimpleNamespace(sleep=slept.append))
    return slept


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
                    "head_branch": "feature/x",
                    "status": "in_progress",
                    "object": {"sha": "first"},
                }
            ),
        ),
    )
    assert cli.main(["sw", "--as", ME, "wait", "--on", "mutation", URL]) == 0
    assert idle.wait(store.redis, "sw", ME)["on"] == {
        "kind": "mutation",
        "target": URL,
        "task": next(agent.task for agent in store.agents("sw") if agent.name == ME),
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
            "head_branch": "feature/x",
            "run_started_at": "2026-10-09T10:00:00Z",
            "status": "completed",
            "conclusion": "success",
        },
        report={"files": [], "not_mutated": [], "failed": False},
        missing=False,
        tip="first",
        base="base",
        bases={},
        gone=(),
        gone_error=b"gh: Not Found (HTTP 404)",
        scope=None,
        scope_name="scope.json",
        report_name="report.json",
        artifacts=[
            {"id": 7, "name": "mutation-preflight-report", "expired": False, "created_at": report_at},
            {"id": 6, "name": "mutation-preflight-report", "expired": False, "created_at": report_at},
            {"id": 9, "name": "other-report", "expired": False, "created_at": report_at},
            {"id": 8, "name": "mutation-preflight-report", "expired": True, "created_at": report_at},
        ],
        archives={},
        jobs=[],
        pr_runs=[],
        requests=[],
    )

    def api(args, **kwargs):
        assert args[:2] == ["gh", "api"]
        assert 0 < kwargs.get("timeout", float("inf")) <= 20
        state.requests.append(args[-1])
        if args[-1].endswith("/zip"):
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as zipped:
                report = state.archives.get(int(args[-1].split("/")[-2]), state.report)
                zipped.writestr(state.report_name, json.dumps(report))
                if state.scope is not None:
                    zipped.writestr(state.scope_name, json.dumps(state.scope))
            output = data.getvalue()
        elif "artifacts?" in args[-1]:
            output = json.dumps({"artifacts": [] if state.missing else state.artifacts})
        elif "/jobs?" in args[-1]:
            output = json.dumps({"jobs": state.jobs})
        elif "/actions/workflows/test.yml/runs?" in args[-1]:
            output = json.dumps({"workflow_runs": state.pr_runs})
        elif "/git/ref/heads/" in args[-1]:
            output = json.dumps({"object": {"sha": state.tip}})
        elif "/compare/" in args[-1]:
            ref = args[-1].split("/compare/")[1].split("...")[0]
            if ref in state.gone:
                return subprocess.CompletedProcess(args, 1, b"", state.gone_error)
            output = json.dumps({"merge_base_commit": {"sha": state.bases.get(ref, state.base)}})
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
        "repos/org/repo/git/ref/heads/feature/x",
        "repos/org/repo/actions/runs/123/artifacts?per_page=100",
        "repos/org/repo/actions/artifacts/7/zip",
    ]


@pytest.fixture(params=["pull-request", "plan"])
def open_pr(preflight, request):
    preflight.missing = True
    preflight.jobs = [
        {
            "name": request.param,
            "conclusion": "success",
            "steps": [{"name": "Record the revisions the report grades", "conclusion": "skipped"}],
        },
    ]
    if request.param == "pull-request":
        preflight.jobs.append({"name": "plan", "conclusion": "skipped"})
    preflight.pr_runs = [
        {"html_url": "wrong", "head_sha": "old", "pull_requests": [{"base": {"ref": "dev"}}]},
        {"html_url": "wrong", "head_sha": "first", "pull_requests": [{"base": {"ref": "main"}}]},
        {
            "html_url": "https://github.com/org/repo/actions/runs/456",
            "head_sha": "first",
            "pull_requests": [{"base": {"ref": "dev"}}],
        },
    ]
    return preflight


def test_the_tick_reports_an_open_pull_request_skip_with_its_run(tick, open_pr, pauses):  # noqa: F811
    held = {**waits.on("mutation", URL), "head": "first"}
    idle.declare_wait(tick.store.redis, "sw", ME, 10_000_000, "", 1, on=held)
    assert tick.end() == [
        f"ended the wait of {ME}: mutation preflight {URL}, now skipped; "
        "the open pull request runs mutation in https://github.com/org/repo/actions/runs/456"
    ]
    assert idle.wait(tick.store.redis, "sw", ME) is None
    assert len(tick.told()) == 1
    assert tick.end() == []
    assert pauses == []


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("run", "path", ".github/workflows/proofs.yml"),
        ("run", "conclusion", "failure"),
        ("job", "name", "refine"),
        ("job", "conclusion", "failure"),
        ("step", "name", "another step"),
        ("step", "conclusion", "success"),
    ],
)
def test_missing_evidence_without_the_successful_branch_skip_stays_red(open_pr, section, field, value):
    targets = {"run": open_pr.run, "job": open_pr.jobs[0], "step": open_pr.jobs[0]["steps"][0]}
    targets[section][field] = value
    if field == "path":
        open_pr.run["event"] = "workflow_dispatch"
    held = {**waits.on("mutation", URL), "head": "first"}
    assert "now red; complete mutation report unavailable" in waits.resolution(held, {}, None, None, None, False)


def test_a_skip_waits_until_its_matching_pull_request_run_is_visible(open_pr, pauses):
    open_pr.pr_runs = open_pr.pr_runs[:2]
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == ""
    assert pauses == []


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
    assert tick.told() == [
        f"Your wait on mutation preflight {URL}, now red; "
        "scripts/swarm/waits.py:mutant_one:aaa (survived); "
        "scripts/swarm/waits.py:mutant_two:bbb (no tests); "
        "scripts/swarm/cli.py: budget exhausted has ended. Pick task t1 back up: "
        "agentihooks swarm sw done, block, or wait on the next thing."
    ]
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


def test_a_successful_run_without_a_mutation_report_ends_red_after_the_retries(preflight, pauses):
    preflight.missing = True
    held = {**waits.on("mutation", URL), "head": "first"}
    assert "now red; complete mutation report unavailable" in waits.resolution(held, {}, None, None, None, False)
    assert pauses == list(mutation_wait.BACKOFF)


def _shards(preflight, *reports, conclusion="failure", summary="cancelled"):
    at = "2026-10-09T10:01:00Z"
    preflight.run["conclusion"] = conclusion
    preflight.jobs = [
        {"name": "plan", "conclusion": "success", "steps": []},
        {"name": "touched-tests", "conclusion": "success", "steps": []},
        {"name": "mutation-shard (0)", "conclusion": "success", "steps": []},
        {"name": "mutation", "conclusion": summary, "steps": []},
    ]
    preflight.artifacts = [
        {"id": 20 + index, "name": f"mutation-preflight-shard-{index}", "expired": False, "created_at": at}
        for index in range(len(reports))
    ] + [{"id": 30, "name": "mutation-killers-0", "expired": False, "created_at": at}]
    preflight.archives = {20 + index: report for index, report in enumerate(reports)}


def _shard(index, total, complete=True, failures=()):
    return {
        "files": [
            {
                "path": f"scripts/s{index}.py",
                "counts": {"killed": 3},
                "failures": list(failures),
                "untouched_survivors": [],
                "cleared": [],
            }
        ],
        "not_mutated": [],
        "failed": bool(failures),
        "shard": {"index": index, "total": total, "complete": complete},
    }


@pytest.mark.parametrize("conclusion", ["failure", "cancelled"])
def test_complete_shard_reports_grade_a_run_whose_summary_was_cancelled(preflight, conclusion):
    _shards(preflight, _shard(0, 2), _shard(1, 2), conclusion=conclusion)
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == (
        f"mutation preflight {URL}, now green; no failing mutants"
    )
    assert "repos/org/repo/actions/artifacts/30/zip" not in preflight.requests


def test_complete_shard_reports_name_their_survivors_when_the_summary_was_cancelled(preflight):
    survivor = {"name": "f__mutmut_1", "fingerprint": "abc", "status": "survived"}
    _shards(preflight, _shard(0, 2), _shard(1, 2, failures=[survivor]))
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == (
        f"mutation preflight {URL}, now red; scripts/s1.py:f__mutmut_1:abc (survived)"
    )


@pytest.mark.parametrize(
    "reports",
    [
        (_shard(0, 2),),
        (_shard(0, 2), _shard(1, 2, complete=False)),
        (_shard(0, 1), _shard(1, 2)),
        ({"files": [], "not_mutated": [], "failed": False},),
    ],
)
def test_missing_or_incomplete_shard_reports_cannot_pass(preflight, reports):
    _shards(preflight, *reports)
    held = {**waits.on("mutation", URL), "head": "first"}
    assert "now red; complete mutation report unavailable" in waits.resolution(held, {}, None, None, None, False)


def test_shard_reports_cannot_pass_a_run_with_another_failed_job(preflight):
    _shards(preflight, _shard(0, 1))
    preflight.jobs[1]["conclusion"] = "failure"
    held = {**waits.on("mutation", URL), "head": "first"}
    assert "now red; complete mutation report unavailable" in waits.resolution(held, {}, None, None, None, False)


def test_split_unit_shard_reports_cannot_pass_without_their_clearances(preflight):
    _shards(preflight, {**_shard(0, 1), "units": [{"path": "scripts/s0.py", "lines": [1], "rows": []}]})
    held = {**waits.on("mutation", URL), "head": "first"}
    assert "now red; complete mutation report unavailable" in waits.resolution(held, {}, None, None, None, False)


def test_a_cancelled_run_without_complete_evidence_ends_red_as_cancelled(preflight):
    _shards(preflight, _shard(0, 2), conclusion="cancelled")
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == f"mutation preflight {URL}, now red; run cancelled"


def test_a_cancelled_run_with_its_summary_report_ends_red_as_cancelled(preflight):
    preflight.run["conclusion"] = "cancelled"
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == f"mutation preflight {URL}, now red; run cancelled"


def test_a_focused_proof_cannot_pass_from_shard_reports(preflight):
    _shards(preflight, _shard(0, 1))
    preflight.run.update({"path": ".github/workflows/proofs.yml", "event": "workflow_dispatch"})
    held = {**waits.on("mutation", URL), "head": "first"}
    assert "now red; complete mutation report unavailable" in waits.resolution(held, {}, None, None, None, False)


@pytest.mark.parametrize("conclusion", ["timed_out", "skipped"])
def test_an_incomplete_run_ends_red_without_claiming_a_pass(preflight, conclusion):
    preflight.run["conclusion"] = conclusion
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == (
        f"mutation preflight {URL}, now red; run {conclusion}"
    )
    assert len(preflight.requests) == 2


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
    reason = (
        "cannot read the mutation preflight head; retry the mutation wait"
        if field == "head_sha"
        else "wait on mutation needs a readable branch Mutation preflight or focused mutation proof run url"
    )
    assert capsys.readouterr().err.endswith(f"swarm: {reason}\n")
    assert idle.wait(store.redis, "sw", ME) is None


@pytest.mark.parametrize("conclusion,failed", [("failure", False), ("success", True), ("failure", True)])
def test_a_failed_run_or_report_cannot_pass_without_named_survivors(preflight, conclusion, failed):
    preflight.run["conclusion"] = conclusion
    preflight.report["failed"] = failed
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == (
        f"mutation preflight {URL}, now red; mutation failed without a named survivor"
    )


def test_a_corrupt_report_download_on_a_green_run_is_unknown_not_red(preflight, monkeypatch, pauses):
    original = subprocess.run
    downloads = []

    def api(args, **kwargs):
        if args[-1].endswith("/zip"):
            downloads.append(args[-1])
            return subprocess.CompletedProcess(args, 0, b"unreadable")
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", api)
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == (
        f"mutation preflight {URL}, now unknown; the run concluded success but its mutation report "
        "could not be fetched: File is not a zip file; wait on the run again"
    )
    assert pauses == list(mutation_wait.BACKOFF)
    assert len(downloads) == len(mutation_wait.BACKOFF) + 1


def test_a_truncated_report_download_is_unknown_not_red(preflight, monkeypatch):
    original = subprocess.run

    def api(args, **kwargs):
        done = original(args, **kwargs)
        if args[-1].endswith("/zip"):
            return subprocess.CompletedProcess(args, 0, done.stdout[: len(done.stdout) // 2])
        return done

    monkeypatch.setattr(subprocess, "run", api)
    held = {**waits.on("mutation", URL), "head": "first"}
    outcome = waits.resolution(held, {}, None, None, None, False)
    assert outcome.startswith(f"mutation preflight {URL}, now unknown; the run concluded success")
    assert "now red" not in outcome


def test_a_corrupt_compressed_report_body_is_unknown_not_red(preflight, monkeypatch):
    original = subprocess.run
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("report.json", json.dumps(preflight.report))
    archive = bytearray(data.getvalue())
    body = 30 + len("report.json")
    archive[body : body + 4] = b"\xff\xff\xff\xff"

    def api(args, **kwargs):
        if args[-1].endswith("/zip"):
            return subprocess.CompletedProcess(args, 0, bytes(archive))
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", api)
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False).startswith(
        f"mutation preflight {URL}, now unknown; the run concluded success"
    )


def test_a_fetch_error_on_a_failed_run_is_red_by_its_conclusion(preflight, monkeypatch):
    preflight.run["conclusion"] = "failure"
    original = subprocess.run

    def api(args, **kwargs):
        if args[-1].endswith("/zip"):
            return subprocess.CompletedProcess(args, 1, b"", b"HTTP 502")
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", api)
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == (
        f"mutation preflight {URL}, now red; run failure; its mutation report could not be fetched: "
        "gh api repos/org/repo/actions/artifacts/7/zip: HTTP 502"
    )


def test_undecodable_gh_stderr_is_reported_with_replacement_characters(preflight, monkeypatch):
    preflight.run["conclusion"] = "failure"
    original = subprocess.run

    def api(args, **kwargs):
        if args[-1].endswith("/zip"):
            return subprocess.CompletedProcess(args, 1, b"", b"HTTP \xff502")
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", api)
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False).endswith(
        "gh api repos/org/repo/actions/artifacts/7/zip: HTTP �502"
    )


def test_an_unparseable_artifact_listing_is_unknown_with_its_parse_error(preflight, monkeypatch):
    original = subprocess.run

    def api(args, **kwargs):
        if "artifacts?" in args[-1]:
            return subprocess.CompletedProcess(args, 0, b"<html>")
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", api)
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == (
        f"mutation preflight {URL}, now unknown; the run concluded success but its mutation report could not be "
        "fetched: gh api repos/org/repo/actions/runs/123/artifacts?per_page=100: Expecting value: line 1 column 1 "
        "(char 0); wait on the run again"
    )


def test_a_report_listed_late_is_read_on_a_retry(preflight, monkeypatch, pauses):
    original = subprocess.run
    listings = []

    def api(args, **kwargs):
        if "artifacts?" in args[-1]:
            listings.append(args[-1])
            preflight.missing = len(listings) == 1
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", api)
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == (
        f"mutation preflight {URL}, now green; no failing mutants"
    )
    assert pauses == list(mutation_wait.BACKOFF[:1])


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


def test_a_report_created_at_the_attempt_start_can_pass(preflight):
    preflight.run["run_started_at"] = "2026-10-09T10:01:00Z"
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == (
        f"mutation preflight {URL}, now green; no failing mutants"
    )


@pytest.mark.parametrize("stamp", [None, "unreadable"])
def test_an_attempt_without_a_readable_start_cannot_pass(preflight, stamp):
    preflight.run["run_started_at"] = stamp
    held = {**waits.on("mutation", URL), "head": "first"}
    assert "complete mutation report unavailable" in waits.resolution(held, {}, None, None, None, False)


def test_utf8_json_is_parsed_without_process_locale_decoding(preflight, monkeypatch):
    preflight.run["display_title"] = "café"
    original = subprocess.run

    def api(args, **kwargs):
        if "/actions/runs/123" in args[-1] and "artifacts" not in args[-1]:
            output = json.dumps(preflight.run, ensure_ascii=False).encode()
            if kwargs.get("text"):
                output = output.decode("ascii")
            return subprocess.CompletedProcess(args, 0, output)
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", api)
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == (
        f"mutation preflight {URL}, now green; no failing mutants"
    )


def _focused_proof(preflight):
    preflight.run.update(path=".github/workflows/proofs.yml", event="workflow_dispatch")
    preflight.scope = {"base": "base", "head": "first"}
    preflight.report["files"] = [{"path": "scripts/swarm/waits.py", "failures": []}]


def _bind(env, preflight):  # noqa: F811
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    ledger.tasks = lambda slug: list(ledger.rows.values())
    return cli.main(["sw", "--as", ME, "wait", "--on", "mutation", URL])


def test_cli_binds_a_focused_proof_run_on_the_branch_head(env, preflight):  # noqa: F811
    _focused_proof(preflight)
    assert _bind(env, preflight) == 0
    task = next(agent.task for agent in env[0].agents("sw") if agent.name == ME)
    assert idle.wait(env[0].redis, "sw", ME)["on"] == {"kind": "mutation", "target": URL, "task": task, "head": "first"}
    assert preflight.requests == ["repos/org/repo/actions/runs/123", "repos/org/repo/git/ref/heads/feature/x"]


@pytest.mark.parametrize("focused", [True, False])
def test_cli_refuses_a_run_on_a_stale_head(env, preflight, capsys, focused):  # noqa: F811
    if focused:
        _focused_proof(preflight)
    preflight.tip = "second"
    assert _bind(env, preflight) == 1
    assert capsys.readouterr().err.endswith(
        "swarm: the mutation run grades a stale head; wait on a run for the branch head\n"
    )
    assert idle.wait(env[0].redis, "sw", ME) is None


def test_a_focused_proof_run_grading_the_current_head_ends_green(preflight):
    _focused_proof(preflight)
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == (
        f"mutation preflight {URL}, now green; no failing mutants"
    )
    assert "repos/org/repo/compare/dev...first" in preflight.requests


@pytest.mark.parametrize("focused", [True, False])
def test_a_run_ends_red_once_the_branch_moves_past_it(preflight, focused):
    if focused:
        _focused_proof(preflight)
    preflight.tip = "second"
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == (
        f"mutation preflight {URL}, now red; the branch moved past the graded head"
    )


@pytest.mark.parametrize(
    "scope",
    [None, {"base": "later", "head": "first"}, {"base": "base", "head": "other"}, {"base": "base"}],
)
def test_a_focused_proof_report_that_does_not_grade_the_head_from_its_merge_base_ends_red(preflight, scope):
    _focused_proof(preflight)
    preflight.scope = scope
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == (
        f"mutation preflight {URL}, now red; the mutation report does not grade the branch head from its dev merge base"
    )


def test_a_focused_proof_reads_only_the_recorded_scope_at_the_report_root(preflight):
    _focused_proof(preflight)
    preflight.scope_name = "work/scope.json"
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == (
        f"mutation preflight {URL}, now red; the mutation report does not grade the branch head from its dev merge base"
    )


STACKED = {
    "t1": {"id": "t1", "depends_on": ["t0", "t9"]},
    "t0": {"id": "t0", "state": "pr", "branch": "engineer-0"},
    "t9": {"id": "t9", "state": "done", "branch": "engineer-9"},
}


def _stacked_proof(preflight):
    _focused_proof(preflight)
    preflight.bases = {"dev": "base", "engineer-0": "stacked", "engineer-9": "merged"}
    preflight.scope = {"base": "stacked", "head": "first"}


def test_the_tick_ends_a_focused_proof_based_on_an_open_dependency_branch_green(tick, preflight):  # noqa: F811
    _stacked_proof(preflight)
    idle.declare_wait(
        tick.store.redis, "sw", ME, 10_000_000, "", 1, on={**waits.on("mutation", URL, "t1"), "head": "first"}
    )
    assert tick.end(STACKED) == [f"ended the wait of {ME}: mutation preflight {URL}, now green; no failing mutants"]
    assert "repos/org/repo/compare/engineer-0...first" in preflight.requests
    assert not any("engineer-9" in request for request in preflight.requests)


def test_a_red_focused_proof_based_on_an_open_dependency_branch_ends_red(preflight):
    _stacked_proof(preflight)
    preflight.run["conclusion"] = "failure"
    preflight.report["failed"] = True
    preflight.report["files"] = [
        {"path": "scripts/swarm/waits.py", "failures": [{"name": "f", "fingerprint": "x", "status": "survived"}]}
    ]
    held = {**waits.on("mutation", URL, "t1"), "head": "first"}
    assert waits.resolution(held, STACKED, None, None, None, False) == (
        f"mutation preflight {URL}, now red; scripts/swarm/waits.py:f:x (survived)"
    )


@pytest.mark.parametrize(
    "base, task, gone",
    [("merged", "t1", ()), ("stacked", "", ()), ("stacked", "t7", ()), ("stacked", "t1", ("engineer-0",))],
)
def test_a_focused_proof_based_on_no_open_dependency_branch_ends_red(preflight, base, task, gone):
    _stacked_proof(preflight)
    preflight.scope["base"] = base
    preflight.gone = gone
    held = {**waits.on("mutation", URL, task), "head": "first"}
    assert waits.resolution(held, STACKED, None, None, None, False) == (
        f"mutation preflight {URL}, now red; the mutation report does not grade the branch head from its dev merge base"
    )


def test_a_rate_limited_dependency_compare_holds_the_wait(preflight, monkeypatch):
    _stacked_proof(preflight)
    scope = mutation_wait._scope

    def limited(repo, ref, run):
        if ref == "engineer-0":
            raise mutation_wait.RateLimited("limited")
        return scope(repo, ref, run)

    monkeypatch.setattr(mutation_wait, "_scope", limited)
    held = {**waits.on("mutation", URL, "t1"), "head": "first"}
    assert waits.resolution(held, STACKED, None, None, None, False) == ""


def test_a_failed_dependency_compare_other_than_not_found_holds_the_wait(preflight):
    _stacked_proof(preflight)
    preflight.gone = ("engineer-0",)
    preflight.gone_error = b"gh: Server Error (HTTP 502)"
    held = {**waits.on("mutation", URL, "t1"), "head": "first"}
    assert waits.resolution(held, STACKED, None, None, None, False) == ""


def test_a_focused_proof_run_of_another_proof_ends_red_without_a_report(preflight):
    _focused_proof(preflight)
    preflight.missing = True
    held = {**waits.on("mutation", URL), "head": "first"}
    assert "now red; complete mutation report unavailable" in waits.resolution(held, {}, None, None, None, False)


def test_a_push_preflight_passes_without_a_recorded_scope(preflight):
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == (
        f"mutation preflight {URL}, now green; no failing mutants"
    )
    assert not any("/compare/" in request for request in preflight.requests)


def test_a_proofs_push_run_is_not_a_focused_mutation_proof(preflight):
    preflight.run["path"] = ".github/workflows/proofs.yml"
    held = {**waits.on("mutation", URL), "head": "first"}
    assert "run no longer matches" in waits.resolution(held, {}, None, None, None, False)


@pytest.fixture
def unreadable(preflight, monkeypatch):
    original = subprocess.run
    state = SimpleNamespace(endpoint="/git/ref/heads/")

    def api(args, **kwargs):
        if state.endpoint in args[-1]:
            return subprocess.CompletedProcess(args, 1, b"")
        return original(args, **kwargs)

    _focused_proof(preflight)
    monkeypatch.setattr(subprocess, "run", api)
    return state


def test_cli_refuses_a_run_whose_branch_is_unreadable(env, preflight, unreadable, capsys):  # noqa: F811
    assert _bind(env, preflight) == 1
    assert capsys.readouterr().err.endswith("swarm: cannot read the branch head; retry the mutation wait\n")
    assert idle.wait(env[0].redis, "sw", ME) is None


def test_a_completed_run_with_an_unreadable_branch_ends_red(unreadable):
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == (
        f"mutation preflight {URL}, now red; the branch head is unreadable"
    )


def test_an_unreachable_merge_base_keeps_the_wait(unreadable):
    unreadable.endpoint = "/compare/"
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == ""


@pytest.mark.parametrize("endpoint", ["artifacts?", "/zip"])
def test_a_failed_report_download_is_retried_before_the_tick_ends_the_wait(tick, preflight, monkeypatch, endpoint):  # noqa: F811
    original = subprocess.run
    failed = []

    def api(args, **kwargs):
        if endpoint in args[-1] and not failed:
            failed.append(args[-1])
            return subprocess.CompletedProcess(args, 1, b"")
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", api)
    held = {**waits.on("mutation", URL), "head": "first"}
    idle.declare_wait(tick.store.redis, "sw", ME, 10_000_000, "", 1, on=held)
    assert tick.end() == [f"ended the wait of {ME}: mutation preflight {URL}, now green; no failing mutants"]
    assert len(failed) == 1


@pytest.mark.parametrize("endpoint", ["artifacts?", "/zip"])
def test_a_report_download_that_keeps_failing_ends_the_wait_unknown(tick, preflight, monkeypatch, endpoint):  # noqa: F811
    original = subprocess.run

    def api(args, **kwargs):
        if endpoint in args[-1]:
            return subprocess.CompletedProcess(args, 1, b"")
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", api)
    held = {**waits.on("mutation", URL), "head": "first"}
    idle.declare_wait(tick.store.redis, "sw", ME, 10_000_000, "", 1, on=held)
    (ended,) = tick.end()
    assert ended.startswith(f"ended the wait of {ME}: mutation preflight {URL}, now unknown; the run concluded success")
    assert "exited 1; wait on the run again" in ended


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired(["gh"], 20), OSError("reset")])
def test_a_timed_out_report_download_is_unknown_not_red(preflight, monkeypatch, error):
    original = subprocess.run

    def api(args, **kwargs):
        if args[-1].endswith("/zip"):
            raise error
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", api)
    held = {**waits.on("mutation", URL), "head": "first"}
    assert waits.resolution(held, {}, None, None, None, False) == (
        f"mutation preflight {URL}, now unknown; the run concluded success but its mutation report could not be "
        f"fetched: gh api repos/org/repo/actions/artifacts/7/zip: {error}; wait on the run again"
    )
