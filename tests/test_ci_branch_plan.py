import io
import json
import re
import subprocess
import zipfile

import pytest

from scripts import ci_branch_plan
from tests import dep_manifest

pytestmark = pytest.mark.unit

REPOSITORY = "the-cloud-clockwork/agentihooks"
ACTIONS = f"repos/{REPOSITORY}/actions"
PULL = 42
BRANCH = "feature/reuse"
THIS_RUN = 900
CREATED = "2026-10-27T12:00:00Z"
ENVIRONMENT = "venv-py3.12-abc"
LISTING = f"{ACTIONS}/workflows/test.yml/runs?branch=feature%2Freuse&event=pull_request&per_page=30"
UNITS = ["hooks/mod.py::<module>", "hooks/mod.py::f", "hooks/mod.py::g", "hooks/mod.py::h"]
# Dependencies measured at the record commit, where f calls g.
DEPENDS = {"test_f": ["f", "g"], "test_g": ["g"], "test_h": ["h"]}
EMPTY = {"version": 1, "runs": [], "tests": []}


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def _source(f="1", g="2", h="3"):
    return f"def f():\n    return {f}\n\n\ndef g():\n    return {g}\n\n\ndef h():\n    return {h}\n"


@pytest.fixture(autouse=True)
def _no_workflow_environment(monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    now, sleeps = [0], []

    def nap(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(ci_branch_plan, "monotonic", lambda: now[0])
    monkeypatch.setattr(ci_branch_plan, "sleep", nap)
    return sleeps


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "hooks").mkdir(parents=True)
    _git(root, "init", "-q")
    commits = {}
    for name, source in (
        ("dev", _source()),
        ("earlier", _source(h="30")),
        ("record", _source(f="g() - 1")),
        ("head", _source(f="g() - 1", g="4")),
    ):
        (root / "hooks" / "mod.py").write_text(source)
        _git(root, "add", ".")
        _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", name)
        commits[name] = _git(root, "rev-parse", "HEAD")
    return root, commits


def _ids(*names):
    return [f"tests/test_mod.py::{name}" for name in names]


def _manifest(commit, depends):
    tests = {
        f"tests/test_mod.py::{name}": {
            "units": [0, *(UNITS.index(f"hooks/mod.py::{unit}") for unit in units)],
            "files": [],
            "escapes": False,
        }
        for name, units in depends.items()
    }
    return {"version": 2, "commit": commit, "environment": ENVIRONMENT, "units": UNITS, "tests": tests}


def _record(run, commit, *, passed=(), failed=(), flaky=(), depends=DEPENDS, **changes):
    return {
        "version": 1,
        "run": run["id"],
        "attempt": 1,
        "leg": "1",
        "commit": commit,
        "head": run["head_sha"],
        "pull": PULL,
        "environment": ENVIRONMENT,
        "manifest": _manifest(commit, depends),
        "passed": _ids(*passed),
        "failed": _ids(*failed),
        "flaky": _ids(*flaky),
        **changes,
    }


def _run(run_id, **changes):
    return {
        "id": run_id,
        "run_attempt": 1,
        "event": "pull_request",
        "head_branch": BRANCH,
        "head_sha": f"{run_id:040x}",
        "path": ".github/workflows/test.yml",
        "head_repository": {"full_name": REPOSITORY},
        "pull_requests": [{"number": PULL}],
        "created_at": f"2026-10-27T11:{run_id % 60:02d}:00Z",
        "status": "completed",
        "conclusion": "cancelled",
        **changes,
    }


def _zip(content, name="record.json"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, content if isinstance(content, (bytes, str)) else json.dumps(content))
    return buffer.getvalue()


def _job(leg, attempt=1, status="completed", conclusion="success", kind="unit-selected", step="Run the selected tests"):
    steps = [{"name": "Set up job", "status": "completed", "conclusion": "success"}]
    steps.append({"name": step, "status": status, "conclusion": conclusion})
    return {"name": f"{kind} (3.12, {leg})", "run_attempt": attempt, "steps": steps}


def _leg_job(name):
    leg, attempt = re.fullmatch(r"branch-record-3\.12-(.*)-(\d+)", name).groups()
    return _job(leg, int(attempt))


def _github(monkeypatch, runs, records, jobs=None):
    artifacts, payloads, calls = {}, {}, []
    for run_id, items in records.items():
        for item in items:
            name, payload = item if isinstance(item, tuple) else (None, item)
            name = name or f"branch-record-3.12-{payload['leg']}-{payload['attempt']}"
            payloads[len(payloads) + 1] = payload if isinstance(payload, bytes) else _zip(payload)
            artifacts.setdefault(run_id, []).append({"id": len(payloads), "name": name, "expired": False})
    legs = {run_id: [_leg_job(item["name"]) for item in items] for run_id, items in artifacts.items()} | (jobs or {})

    def api(endpoint):
        calls.append(endpoint)
        if endpoint == LISTING:
            return json.dumps({"workflow_runs": runs() if callable(runs) else runs}).encode()
        if endpoint.endswith("/zip"):
            return payloads[int(endpoint.split("/artifacts/")[1].split("/")[0])]
        run_id = int(endpoint.split("/runs/")[1].split("/")[0])
        page = int(endpoint.rpartition("&page=")[2])
        if "/attempts/" in endpoint:
            attempt = int(endpoint.split("/attempts/")[1].split("/")[0])
            assert endpoint == f"{ACTIONS}/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}"
            found = legs.get(run_id, [])
            found = found.get(attempt, []) if isinstance(found, dict) else found
            return json.dumps({"jobs": found[(page - 1) * 100 : page * 100]}).encode()
        assert endpoint == f"{ACTIONS}/runs/{run_id}/artifacts?per_page=100&page={page}"
        return json.dumps({"artifacts": artifacts.get(run_id, [])[(page - 1) * 100 : page * 100]}).encode()

    monkeypatch.setattr(ci_branch_plan, "_api", api)
    return calls, artifacts


def _args(tmp_path, root, *extra, event="pull_request"):
    args = ["--repository", REPOSITORY, "--pull", str(PULL), "--head-ref", BRANCH, "--head-repo", REPOSITORY]
    args += ["--run", str(THIS_RUN), "--created", CREATED, "--environment", ENVIRONMENT]
    args += ["--root", str(root), "--out", str(tmp_path / "reused-branch.json"), *extra]
    return args if event is None else [*args, "--event", event]


def _plan(tmp_path, root, *extra, event="pull_request"):
    assert ci_branch_plan.main(_args(tmp_path, root, *extra, event=event)) == 0
    return json.loads((tmp_path / "reused-branch.json").read_text())


def _downloads(calls):
    return [endpoint for endpoint in calls if endpoint.endswith("/zip")]


def test_a_test_whose_function_newly_calls_a_changed_function_runs_again(tmp_path, monkeypatch, repo):
    root, commits = repo
    dev = _manifest(commits["dev"], {"test_f": ["f"], "test_g": ["g"], "test_h": ["h"]})
    stale, _ = dep_manifest.invalidated(dev, dep_manifest.changes(root, commits["record"], "HEAD"))
    assert "tests/test_mod.py::test_f" not in stale
    run = _run(101)
    _github(monkeypatch, [run], {101: [_record(run, commits["record"], passed=["test_f", "test_g", "test_h"])]})
    plan = _plan(tmp_path, root)
    assert plan == {
        "version": 1,
        "runs": [
            {"run": 101, "attempt": 1, "commit": commits["record"], "head": run["head_sha"], "tests": _ids("test_h")}
        ],
        "tests": _ids("test_h"),
    }


def test_a_passed_test_without_dependency_evidence_is_not_reused(tmp_path, monkeypatch, repo):
    root, commits = repo
    run = _run(101)
    _github(monkeypatch, [run], {101: [_record(run, commits["record"], passed=["test_h", "test_unmeasured"])]})
    assert _plan(tmp_path, root)["tests"] == _ids("test_h")


@pytest.mark.parametrize("outcome", ["failed", "flaky"])
@pytest.mark.parametrize("failing", [101, 102])
def test_a_failure_on_any_record_that_still_holds_vetoes_a_pass(tmp_path, monkeypatch, repo, capsys, outcome, failing):
    root, commits = repo
    runs = [_run(102), _run(101)]
    records = {
        run["id"]: [_record(run, commits["record"], **{outcome if run["id"] == failing else "passed": ["test_h"]})]
        for run in runs
    }
    _github(monkeypatch, runs, records)
    assert _plan(tmp_path, root) == EMPTY
    assert "Pull request run 101 on" in capsys.readouterr().out


def test_a_failure_the_head_has_since_invalidated_does_not_veto(tmp_path, monkeypatch, repo):
    root, commits = repo
    newer, older = _run(102), _run(101)
    _github(
        monkeypatch,
        [newer, older],
        {
            102: [_record(newer, commits["record"], passed=["test_h"])],
            101: [_record(older, commits["earlier"], failed=["test_h"], flaky=["test_h"])],
        },
    )
    assert _plan(tmp_path, root)["tests"] == _ids("test_h")


@pytest.mark.parametrize("outcome", ["failed", "flaky"])
def test_a_failure_without_dependency_evidence_vetoes_a_pass(tmp_path, monkeypatch, repo, outcome):
    root, commits = repo
    newer, older = _run(102), _run(101)
    unmeasured = {"test_f": ["f", "g"]}
    _github(
        monkeypatch,
        [newer, older],
        {
            102: [_record(newer, commits["record"], passed=["test_h"])],
            101: [_record(older, commits["record"], **{outcome: ["test_h"]}, depends=unmeasured)],
        },
    )
    assert _plan(tmp_path, root) == EMPTY


def test_the_head_repository_matches_without_regard_to_case(tmp_path, monkeypatch, repo):
    root, commits = repo
    run = _run(101, head_repository={"full_name": REPOSITORY.upper()})
    _github(monkeypatch, [run], {101: [_record(run, commits["record"], passed=["test_h"])]})
    assert _plan(tmp_path, root)["tests"] == _ids("test_h")


def test_each_reused_test_is_attributed_to_the_newest_run_that_passed_it(tmp_path, monkeypatch, repo, capsys):
    root, commits = repo
    newer, older = _run(102), _run(101)
    _github(
        monkeypatch,
        [newer, older],
        {
            102: [_record(newer, commits["record"], passed=["test_h"])],
            101: [_record(older, commits["record"], passed=["test_h"])],
        },
    )
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    plan = _plan(tmp_path, root)
    assert [entry["run"] for entry in plan["runs"]] == [102]
    assert plan["tests"] == _ids("test_h")
    assert output.read_text() == "branch=1\nbranch_runs=102\n"
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        f"Pull request run 102 on {commits['record']}: 1 reused",
        f"Pull request run 101 on {commits['record']}: 0 reused",
    ]
    assert re.fullmatch(r"The branch planner made \d+ GitHub API calls\n", captured.err)


def test_a_test_claimed_by_a_newer_run_is_never_claimed_again(tmp_path, monkeypatch, repo):
    root, commits = repo
    runs = [_run(103), _run(102), _run(101)]
    depends = {**DEPENDS, "test_x": []}
    records = {
        103: [_record(runs[0], commits["record"], passed=["test_h"])],
        102: [_record(runs[1], commits["record"], passed=["test_x"], depends=depends)],
        101: [_record(runs[2], commits["record"], passed=["test_h", "test_x"], depends=depends)],
    }
    _github(monkeypatch, runs, records)
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    plan = _plan(tmp_path, root)
    assert [(entry["run"], entry["tests"]) for entry in plan["runs"]] == [(103, _ids("test_h")), (102, _ids("test_x"))]
    assert plan["tests"] == _ids("test_h", "test_x")
    assert output.read_text() == "branch=2\nbranch_runs=103,102\n"


def test_a_run_whose_attempts_both_contribute_is_named_once(tmp_path, monkeypatch, repo):
    root, commits = repo
    run = _run(101, run_attempt=2)
    first = _record(run, commits["record"], passed=["test_x"], attempt=1, depends={"test_x": []})
    second = _record(run, commits["record"], passed=["test_h"], attempt=2)
    _github(monkeypatch, [run], {101: [first, second]})
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    plan = _plan(tmp_path, root)
    assert [(entry["attempt"], entry["tests"]) for entry in plan["runs"]] == [(2, _ids("test_h")), (1, _ids("test_x"))]
    assert output.read_text() == "branch=2\nbranch_runs=101\n"


def test_the_newest_attempt_of_a_run_claims_its_tests_first(tmp_path, monkeypatch, repo):
    root, commits = repo
    run = _run(101, run_attempt=2)
    first = _record(run, commits["record"], passed=["test_h"], attempt=1)
    second = _record(run, commits["record"], passed=["test_h"], attempt=2)
    _github(monkeypatch, [run], {101: [first, second]})
    assert [(entry["run"], entry["attempt"]) for entry in _plan(tmp_path, root)["runs"]] == [(101, 2)]


def test_records_of_several_legs_join_one_entry_per_run_attempt(tmp_path, monkeypatch, repo):
    root, commits = repo
    run = _run(101)
    legs = [
        _record(run, commits["record"], passed=["test_h"], leg="1"),
        _record(run, commits["record"], passed=["test_x"], leg="2", depends={"test_x": []}),
    ]
    _github(monkeypatch, [run], {101: legs})
    plan = _plan(tmp_path, root)
    assert [entry["tests"] for entry in plan["runs"]] == [_ids("test_h", "test_x")]
    assert plan["tests"] == _ids("test_h", "test_x")


@pytest.mark.parametrize("present", [True, False])
def test_tests_the_dev_plan_reuses_stay_attributed_to_dev(tmp_path, monkeypatch, repo, present):
    root, commits = repo
    run = _run(101)
    record = _record(run, commits["record"], passed=["test_h", "test_x"], depends={**DEPENDS, "test_x": []})
    _github(monkeypatch, [run], {101: [record]})
    dev = tmp_path / "reused-tests.json"
    if present:
        dev.write_text(json.dumps({"run": "5", "commit": commits["dev"], "global": None, "tests": _ids("test_h")}))
    plan = _plan(tmp_path, root, "--dev-plan", str(dev))
    assert plan["tests"] == (_ids("test_x") if present else _ids("test_h", "test_x"))


@pytest.mark.parametrize("event", ["merge_group", "push", "workflow_dispatch", ""])
def test_only_a_pull_request_run_reads_branch_records(tmp_path, monkeypatch, repo, capsys, event):
    root, _ = repo
    monkeypatch.setattr(ci_branch_plan, "_api", lambda endpoint: pytest.fail("no branch record may be read"))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert _plan(tmp_path, root, event=event) == EMPTY
    assert output.read_text() == "branch=0\nbranch_runs=\n"
    assert capsys.readouterr().out == (
        f"::notice title=No branch record::A {event or 'unnamed'} run reuses no earlier pull request run.\n"
    )


@pytest.mark.parametrize(("event", "reused"), [(None, []), ("merge_group", []), ("pull_request", _ids("test_h"))])
def test_the_event_defaults_to_the_workflow_event(tmp_path, monkeypatch, repo, capsys, event, reused):
    root, commits = repo
    run = _run(101)
    _github(monkeypatch, [run], {101: [_record(run, commits["record"], passed=["test_h"])]})
    if event:
        monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    assert _plan(tmp_path, root, event=None)["tests"] == reused
    refusal = f"::notice title=No branch record::A {event or 'unnamed'} run reuses no earlier pull request run.\n"
    assert (capsys.readouterr().out == refusal) == (not reused)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pull_requests", [{"number": 7}]),
        ("pull_requests", []),
        ("head_repository", {"full_name": "someone/agentihooks"}),
        ("head_repository", {}),
        ("head_repository", None),
        ("head_branch", "other"),
        ("event", "merge_group"),
        ("path", ".github/workflows/proofs.yml"),
        ("id", THIS_RUN),
        ("created_at", CREATED),
        ("created_at", "2026-10-27T12:00:01Z"),
    ],
)
def test_runs_that_are_not_earlier_tests_runs_of_this_pull_request_are_never_read(
    tmp_path, monkeypatch, repo, capsys, field, value
):
    root, commits = repo
    run = _run(101, **{field: value})
    calls, _ = _github(monkeypatch, [run], {run["id"]: [_record(run, commits["record"], passed=["test_h"])]})
    assert _plan(tmp_path, root) == EMPTY
    assert calls == [LISTING]
    assert capsys.readouterr().out == (
        f"::notice title=No branch record::No earlier Tests run of pull request {PULL} left a usable branch record;"
        " no test is reused from one.\n"
    )


@pytest.mark.parametrize(
    ("changes", "name", "reason"),
    [
        ({"head": "f" * 40}, None, "head differed from its run"),
        ({"run": 55}, None, "run differed from its run"),
        ({"pull": 7}, None, "pull differed from its run"),
        ({"environment": "py3.11"}, None, "environment differed from its run"),
        ({"pull": 7, "environment": "py3.11"}, None, "pull, environment differed from its run"),
        ({"attempt": 2}, None, "its leg and attempt do not match the artifact"),
        ({"leg": "2"}, "branch-record-3.12-1-1", "its leg and attempt do not match the artifact"),
        ({"attempt": 0}, "branch-record-3.12-1-0", "its leg and attempt do not match the artifact"),
        ({"version": 2}, None, "the record schema is invalid"),
        ({"version": True}, None, "the record schema is invalid"),
        ({"run": "101"}, None, "the record schema is invalid"),
        ({"leg": ""}, "branch-record-3.12-1-1", "the record schema is invalid"),
        ({"passed": "tests/test_mod.py::test_h"}, None, "the record schema is invalid"),
        ({"manifest": []}, None, "the record schema is invalid"),
        ({"commit": "HEAD"}, None, "its manifest does not describe its commit"),
    ],
)
def test_a_record_that_disagrees_with_its_run_is_unusable(tmp_path, monkeypatch, repo, capsys, changes, name, reason):
    root, commits = repo
    run = _run(101)
    record = {**_record(run, commits["record"], passed=["test_h"]), **changes}
    name = name or f"branch-record-3.12-{record['leg']}-{record['attempt']}"
    _github(monkeypatch, [run], {101: [(name, record)]})
    assert _plan(tmp_path, root) == EMPTY
    assert f"::warning title=Unusable branch record::Pull request run 101: {name}: {reason};" in capsys.readouterr().out


def test_a_record_whose_manifest_describes_another_commit_is_unusable(tmp_path, monkeypatch, repo, capsys):
    root, commits = repo
    run = _run(101)
    record = _record(run, commits["record"], passed=["test_h"])
    record["manifest"]["commit"] = commits["head"]
    _github(monkeypatch, [run], {101: [record]})
    assert _plan(tmp_path, root) == EMPTY
    assert "its manifest does not describe its commit" in capsys.readouterr().out


def test_a_record_naming_an_option_as_its_commit_never_reaches_git(tmp_path, monkeypatch, repo, capsys):
    root, _ = repo
    run = _run(101)
    record = _record(run, "--upload-pack=touch planted", passed=["test_h"])
    _github(monkeypatch, [run], {101: [record]})
    monkeypatch.setattr(ci_branch_plan.subprocess, "run", lambda *a, **k: pytest.fail("git ran"))
    assert _plan(tmp_path, root) == EMPTY
    assert "its manifest does not describe its commit" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"not a zip", "File is not a zip file"),
        (_zip("{}", name="other.json"), str(KeyError("There is no item named 'record.json' in the archive"))),
        (_zip("{"), "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"),
        (_zip("[]"), "the record schema is invalid"),
        (
            _zip(
                json.dumps({"manifest": {"units": [], "tests": {"t": {"units": [3], "files": [], "escapes": False}}}})
            ),
            "the record schema is invalid",
        ),
    ],
    ids=["corrupt-zip", "missing-record", "corrupt-json", "non-object", "invalid-schema"],
)
def test_an_unusable_download_is_warned_about_and_exits_zero(tmp_path, monkeypatch, repo, capsys, payload, reason):
    root, _ = repo
    _github(monkeypatch, [_run(101)], {101: [("branch-record-3.12-1-1", payload)]})
    assert _plan(tmp_path, root) == EMPTY
    assert capsys.readouterr().out == (
        f"::warning title=Unusable branch record::Pull request run 101: branch-record-3.12-1-1: {reason}; neither it"
        " nor any older run is reused.\n"
        f"::notice title=No branch record::No earlier Tests run of pull request {PULL} left a usable branch record;"
        " no test is reused from one.\n"
    )


def _raise(error):
    raise error


def _gh_error(endpoint):
    return subprocess.CalledProcessError(1, ["gh", "api", endpoint])


def _break(monkeypatch, cause, broken, artifacts, commit):
    api, changes, invalidated = ci_branch_plan._api, dep_manifest.changes, dep_manifest.invalidated
    failing = {"download": f"{ACTIONS}/artifacts/1/zip", "listing": f"{ACTIONS}/runs/{broken}/artifacts?"}.get(cause)

    def fetch(endpoint):
        if failing and endpoint.startswith(failing):
            raise _gh_error(endpoint)
        return api(endpoint)

    monkeypatch.setattr(ci_branch_plan, "_api", fetch)
    if cause == "pages":
        artifacts[broken][:0] = [{"id": 1000 + n, "name": f"other-{n}", "expired": False} for n in range(500)]
    if cause == "diff":
        error = subprocess.CalledProcessError(128, ["git", "diff"])
        monkeypatch.setattr(
            dep_manifest,
            "changes",
            lambda root, base, tip: _raise(error) if base == commit else changes(root, base, tip),
        )
    if cause == "invalidated":
        monkeypatch.setattr(
            dep_manifest,
            "invalidated",
            lambda manifest, changed: (
                _raise(KeyError(9)) if manifest["commit"] == commit else invalidated(manifest, changed)
            ),
        )


@pytest.mark.parametrize(
    "cause", ["unreadable", "invalid", "download", "listing", "pages", "unfetchable", "diff", "invalidated"]
)
@pytest.mark.parametrize("broken", [102, 101])
def test_a_run_that_cannot_be_fully_read_hides_itself_and_every_older_run(
    tmp_path, monkeypatch, repo, capsys, cause, broken
):
    root, commits = repo
    runs = {102: _run(102), 101: _run(101)}
    good = runs[203 - broken]
    defect = _record(runs[broken], "e" * 40 if cause == "unfetchable" else commits["head"], passed=["test_h"])
    payload = {"unreadable": ("branch-record-3.12-1-1", b"broken"), "invalid": {**defect, "head": "f" * 40}}
    records = {broken: [payload.get(cause, defect)], good["id"]: [_record(good, commits["record"], passed=["test_h"])]}
    _, artifacts = _github(monkeypatch, list(runs.values()), records)
    _break(monkeypatch, cause, broken, artifacts, commits["head"])
    plan = _plan(tmp_path, root)
    assert [entry["run"] for entry in plan["runs"]] == ([] if broken == 102 else [102])
    assert plan["tests"] == ([] if broken == 102 else _ids("test_h"))
    reason = {
        "unreadable": "branch-record-3.12-1-1: File is not a zip file",
        "invalid": "branch-record-3.12-1-1: head differed from its run",
        "download": f"branch-record-3.12-1-1: {_gh_error(f'{ACTIONS}/artifacts/1/zip')}",
        "listing": f"CalledProcessError: {_gh_error(f'{ACTIONS}/runs/{broken}/artifacts?per_page=100&page=1')}",
        "pages": "it lists more than 5 pages of artifacts",
        "unfetchable": f"commit {'e' * 40} could not be fetched",
        "diff": f"CalledProcessError: {subprocess.CalledProcessError(128, ['git', 'diff'])}",
        "invalidated": "KeyError: 9",
    }[cause]
    assert (
        f"::warning title=Unusable branch record::Pull request run {broken}: {reason}; neither it nor any older run"
        " is reused.\n"
    ) in capsys.readouterr().out


@pytest.mark.parametrize("outcome", ["failed", "flaky"])
def test_a_run_whose_commit_cannot_be_fetched_still_vetoes_its_failures(tmp_path, monkeypatch, repo, outcome):
    root, commits = repo
    newer, unfetched = _run(103), _run(102)
    records = {
        103: [_record(newer, commits["record"], passed=["test_h"])],
        102: [_record(unfetched, "e" * 40, **{outcome: ["test_h"]}), _record(unfetched, "e" * 40, leg="2")],
    }
    _github(monkeypatch, [newer, unfetched], records)
    assert _plan(tmp_path, root) == EMPTY


@pytest.mark.parametrize(
    ("job", "barrier"),
    [
        (_job("2"), True),
        (_job("2", kind="unit", step="Run tests"), True),
        (_job("2", status="in_progress", conclusion=None), True),
        (_job("2", conclusion="failure"), True),
        (_job("2", conclusion="cancelled"), True),
        (_job("2", status="queued", conclusion=None), False),
        (_job("2", conclusion="skipped"), False),
        (_job("2", step="Upload the coverage"), False),
        ({**_job("2"), "name": "unit (3.11, 2)"}, False),
        ({**_job("2"), "name": "unit-selected (3.12, ٢)"}, False),
    ],
    ids=[
        "passed",
        "full-scope-shard",
        "running",
        "failed",
        "cancelled",
        "queued",
        "skipped",
        "other-step",
        "other-interpreter",
        "non-ascii-leg",
    ],
)
def test_a_started_test_leg_without_its_record_hides_its_run_and_every_older_run(
    tmp_path, monkeypatch, repo, capsys, job, barrier
):
    root, commits = repo
    newer, older = _run(102), _run(101)
    records = {
        102: [_record(newer, commits["record"], passed=["test_x"], depends={**DEPENDS, "test_x": []})],
        101: [_record(older, commits["record"], passed=["test_h"])],
    }
    _github(monkeypatch, [newer, older], records, jobs={102: [_job("1"), job]})
    assert _plan(tmp_path, root)["tests"] == ([] if barrier else _ids("test_h", "test_x"))
    warning = (
        "::warning title=Unusable branch record::Pull request run 102: started test legs left no"
        " branch-record-3.12-2-1; neither it nor any older run is reused.\n"
    )
    assert (warning in capsys.readouterr().out) == barrier


def test_an_expired_record_of_a_started_leg_counts_as_missing(tmp_path, monkeypatch, repo, capsys):
    root, commits = repo
    run = _run(101)
    calls, artifacts = _github(monkeypatch, [run], {101: [_record(run, commits["record"], passed=["test_h"])]})
    artifacts[101][0]["expired"] = True
    assert _plan(tmp_path, root) == EMPTY
    assert _downloads(calls) == []
    assert "Pull request run 101: started test legs left no branch-record-3.12-1-1;" in capsys.readouterr().out


def test_every_started_leg_missing_its_record_is_named(tmp_path, monkeypatch, repo, capsys):
    root, commits = repo
    run = _run(101)
    record = _record(run, commits["record"], passed=["test_h"])
    _github(monkeypatch, [run], {101: [record]}, jobs={101: [_job("1"), _job("2"), _job("3")]})
    assert _plan(tmp_path, root) == EMPTY
    assert (
        "::warning title=Unusable branch record::Pull request run 101: started test legs left no"
        " branch-record-3.12-2-1, branch-record-3.12-3-1; neither it nor any older run is reused.\n"
    ) in capsys.readouterr().out


def test_a_leg_started_in_an_earlier_attempt_still_needs_its_record(tmp_path, monkeypatch, repo, capsys):
    root, commits = repo
    run = _run(101, run_attempt=2)
    record = _record(run, commits["record"], passed=["test_h"], leg="2", attempt=2)
    _github(monkeypatch, [run], {101: [record]}, jobs={101: {1: [_job("1")], 2: [_job("2", attempt=2)]}})
    assert _plan(tmp_path, root) == EMPTY
    assert "Pull request run 101: started test legs left no branch-record-3.12-1-1;" in capsys.readouterr().out


def test_a_run_still_running_at_the_first_poll_is_read_once_it_completes(tmp_path, monkeypatch, repo, clock):
    root, commits = repo
    run = _run(101)
    snapshots = iter([[_run(101, status="in_progress")]])
    calls, _ = _github(
        monkeypatch, lambda: next(snapshots, [run]), {101: [_record(run, commits["record"], passed=["test_h"])]}
    )
    assert _plan(tmp_path, root)["tests"] == _ids("test_h")
    assert clock == [15]
    assert calls.count(LISTING) == 2


@pytest.mark.parametrize("wait", [None, 30])
@pytest.mark.parametrize("pending", [102, 101])
def test_a_run_unfinished_when_the_wait_ends_hides_itself_and_every_older_run(
    tmp_path, monkeypatch, repo, capsys, clock, wait, pending
):
    root, commits = repo
    polls = {None: 17, 30: 3}[wait]
    runs = {102: _run(102), 101: _run(101)}
    records = {run_id: [_record(run, commits["record"], passed=["test_h"])] for run_id, run in runs.items()}
    runs[pending]["status"] = "in_progress"
    calls, _ = _github(monkeypatch, list(runs.values()), records)
    plan = _plan(tmp_path, root, *(() if wait is None else ("--wait-seconds", str(wait))))
    assert [entry["run"] for entry in plan["runs"]] == ([] if pending == 102 else [102])
    assert clock == [15] * (polls - 1)
    assert calls.count(LISTING) == polls
    assert f"{ACTIONS}/runs/{pending}/artifacts?per_page=100&page=1" not in calls
    assert (
        f"::warning title=Unusable branch record::Pull request run {pending}: it was still in_progress after waiting"
        f" {wait or 240} seconds; neither it nor any older run is reused.\n"
    ) in capsys.readouterr().out


@pytest.mark.parametrize(("budget", "reused"), [(6, _ids("test_h")), (5, [])])
def test_every_poll_counts_against_the_request_budget(tmp_path, monkeypatch, repo, budget, reused):
    root, commits = repo
    run = _run(101)
    snapshots = iter([[_run(101, status="queued")]])
    records = [_record(run, commits["record"], passed=["test_h"], leg=leg) for leg in ("1", "2")]
    _github(monkeypatch, lambda: next(snapshots, [run]), {101: records})
    monkeypatch.setattr(ci_branch_plan, "REQUESTS", budget)
    assert _plan(tmp_path, root)["tests"] == reused


def test_a_run_without_test_leg_jobs_contributes_nothing_and_hides_nothing(tmp_path, monkeypatch, repo, capsys):
    root, commits = repo
    newer, older = _run(102), _run(101)
    records = {
        102: [_record(newer, commits["record"], passed=["test_x"], depends={**DEPENDS, "test_x": []})],
        101: [_record(older, commits["record"], passed=["test_h"])],
    }
    _github(monkeypatch, [newer, older], records, jobs={102: []})
    plan = _plan(tmp_path, root)
    assert [(entry["run"], entry["tests"]) for entry in plan["runs"]] == [(101, _ids("test_h"))]
    assert "::warning" not in capsys.readouterr().out


def test_a_job_carried_into_a_later_attempt_needs_only_its_own_attempts_record(tmp_path, monkeypatch, repo):
    root, commits = repo
    run = _run(101, run_attempt=2)
    record = _record(run, commits["record"], passed=["test_h"])
    _github(monkeypatch, [run], {101: [record]}, jobs={101: {1: [_job("1")], 2: [_job("1")]}})
    assert _plan(tmp_path, root)["tests"] == _ids("test_h")


def test_a_leg_started_only_in_a_later_attempt_still_needs_its_record(tmp_path, monkeypatch, repo):
    root, commits = repo
    newer, older = _run(102, run_attempt=2), _run(101)
    records = {
        102: [_record(newer, commits["record"], passed=["test_x"], depends={**DEPENDS, "test_x": []})],
        101: [_record(older, commits["record"], passed=["test_h"])],
    }
    _github(monkeypatch, [newer, older], records, jobs={102: {1: [_job("1")], 2: [_job("2", attempt=2)]}})
    assert _plan(tmp_path, root) == EMPTY


@pytest.mark.parametrize(("head_repo", "reused"), [("someone/agentihooks", []), (REPOSITORY.upper(), _ids("test_h"))])
def test_only_a_pull_request_from_this_repository_reuses_its_earlier_runs(
    tmp_path, monkeypatch, repo, capsys, head_repo, reused
):
    root, commits = repo
    run = _run(101, head_repository={"full_name": head_repo})
    calls, _ = _github(monkeypatch, [run], {101: [_record(run, commits["record"], passed=["test_h"])]})
    args = _args(tmp_path, root)
    args[args.index("--head-repo") + 1] = head_repo
    assert ci_branch_plan.main(args) == 0
    assert json.loads((tmp_path / "reused-branch.json").read_text())["tests"] == reused
    assert (calls == []) == (not reused)
    refusal = (
        f"::notice title=No branch record::Pull request {PULL} comes from {head_repo}, so it reuses no earlier run."
    )
    assert (refusal in capsys.readouterr().out) == (not reused)


@pytest.mark.parametrize("failing", ["/workflows/test.yml/runs?", "/artifacts?per_page", "/zip"])
def test_an_api_failure_still_exits_zero_with_an_empty_plan(tmp_path, monkeypatch, repo, capsys, failing):
    root, commits = repo
    run = _run(101)
    _github(monkeypatch, [run], {101: [_record(run, commits["record"], passed=["test_h"])]})
    api = ci_branch_plan._api

    def fail(endpoint):
        if failing in endpoint:
            raise _gh_error(endpoint)
        return api(endpoint)

    monkeypatch.setattr(ci_branch_plan, "_api", fail)
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert _plan(tmp_path, root) == EMPTY
    assert output.read_text() == "branch=0\nbranch_runs=\n"
    expected = {
        "/workflows/test.yml/runs?": f"CalledProcessError: {_gh_error(LISTING)}; no test is reused from an earlier"
        " pull request run.",
        "/artifacts?per_page": f"Pull request run 101: CalledProcessError: {_gh_error(f'{ACTIONS}/runs/101/artifacts?per_page=100&page=1')};"
        " neither it nor any older run is reused.",
        "/zip": f"Pull request run 101: branch-record-3.12-1-1: {_gh_error(f'{ACTIONS}/artifacts/1/zip')}; neither it"
        " nor any older run is reused.",
    }[failing]
    assert f"::warning title=Unusable branch record::{expected}\n" in capsys.readouterr().out


@pytest.mark.parametrize(
    "listing",
    [
        b"not json",
        b"{}",
        b'{"workflow_runs": [null]}',
        json.dumps({"workflow_runs": [_run(101, pull_requests=[PULL])]}).encode(),
    ],
    ids=["corrupt", "no-runs", "null-run", "bare-pull-number"],
)
def test_an_unreadable_run_listing_still_exits_zero_with_an_empty_plan(tmp_path, monkeypatch, repo, capsys, listing):
    root, _ = repo
    monkeypatch.setattr(ci_branch_plan, "_api", lambda endpoint: listing)
    assert _plan(tmp_path, root) == EMPTY
    assert "::warning title=Unusable branch record::" in capsys.readouterr().out


def test_a_record_commit_the_checkout_cannot_fetch_is_skipped(tmp_path, monkeypatch, repo, capsys):
    root, _ = repo
    run = _run(101)
    _github(monkeypatch, [run], {101: [_record(run, "e" * 40, passed=["test_h"])]})
    assert _plan(tmp_path, root) == EMPTY
    out = capsys.readouterr().out
    assert f"Pull request run 101: commit {'e' * 40} could not be fetched; neither it nor any older run" in out
    assert "::notice title=No branch record::" in out


def test_a_record_commit_missing_from_the_checkout_is_fetched_from_origin(tmp_path, monkeypatch, repo):
    remote, commits = repo
    tree = _git(remote, "rev-parse", f"{commits['record']}^{{tree}}")
    merge = _git(
        remote, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", tree, "-p", commits["dev"], "-m", "m"
    )
    _git(remote, "update-ref", f"refs/pull/{PULL}/merge", merge)
    _git(remote, "config", "uploadpack.allowAnySHA1InWant", "true")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--no-local", str(remote), str(clone))
    assert subprocess.run(["git", "-C", str(clone), "cat-file", "-e", merge], capture_output=True).returncode
    run = _run(101)
    _github(monkeypatch, [run], {101: [_record(run, merge, passed=["test_g", "test_h"])]})
    assert _plan(tmp_path, clone)["tests"] == _ids("test_h")
    _git(clone, "cat-file", "-e", merge)


def test_only_unexpired_branch_records_of_the_graded_interpreter_are_downloaded(tmp_path, monkeypatch, repo):
    root, commits = repo
    run = _run(101)
    calls, artifacts = _github(monkeypatch, [run], {101: [_record(run, commits["record"], passed=["test_h"])]})
    artifacts[101] += [
        {"id": 90, "name": "branch-coverage-3.12-1-1", "expired": False},
        {"id": 91, "name": "branch-record-3.11-1-1", "expired": False},
        {"id": 92, "name": "branch-record-3.12-2-1", "expired": True},
        {"id": 93, "name": "coverage-3.12-1", "expired": False},
    ]
    assert _plan(tmp_path, root)["tests"] == _ids("test_h")
    assert _downloads(calls) == [f"{ACTIONS}/artifacts/1/zip"]


def test_records_listed_past_the_first_artifact_page_are_read(tmp_path, monkeypatch, repo):
    root, commits = repo
    run = _run(101)
    records = [
        _record(run, commits["record"], passed=["test_h"]),
        _record(run, commits["record"], passed=["test_x"], leg="2", depends={"test_x": []}),
    ]
    calls, artifacts = _github(monkeypatch, [run], {101: records})
    artifacts[101][1:1] = [{"id": 1000 + n, "name": f"durations-3.12-{n}", "expired": False} for n in range(100)]
    assert _plan(tmp_path, root)["tests"] == _ids("test_h", "test_x")
    assert f"{ACTIONS}/runs/101/artifacts?per_page=100&page=2" in calls


@pytest.mark.parametrize(("others", "reused"), [(498, _ids("test_h")), (499, [])])
def test_a_run_listing_more_than_five_artifact_pages_is_skipped(tmp_path, monkeypatch, repo, capsys, others, reused):
    root, commits = repo
    run = _run(101)
    calls, artifacts = _github(monkeypatch, [run], {101: [_record(run, commits["record"], passed=["test_h"])]})
    artifacts[101][:0] = [{"id": 1000 + n, "name": f"other-{n}", "expired": False} for n in range(others)]
    assert _plan(tmp_path, root)["tests"] == reused
    assert len([endpoint for endpoint in calls if "/artifacts?" in endpoint]) == 5
    assert ("Pull request run 101: it lists more than 5 pages of artifacts" in capsys.readouterr().out) == (not reused)


def test_only_the_newest_eight_earlier_runs_are_read(tmp_path, monkeypatch, repo):
    root, _ = repo
    calls, _ = _github(monkeypatch, [_run(n) for n in range(112, 100, -1)], {})
    assert _plan(tmp_path, root) == EMPTY
    listed = [int(endpoint.split("/runs/")[1].split("/")[0]) for endpoint in calls if "/artifacts?" in endpoint]
    assert sorted(listed, reverse=True) == list(range(112, 104, -1))


@pytest.mark.parametrize(("legs", "reused"), [(97, _ids("test_h")), (98, [])])
def test_a_run_is_read_only_when_its_records_fit_a_hundred_requests(tmp_path, monkeypatch, repo, capsys, legs, reused):
    root, commits = repo
    run = _run(101)
    records = [_record(run, commits["record"], passed=["test_h"], leg=str(leg)) for leg in range(legs)]
    calls, _ = _github(monkeypatch, [run], {101: records})
    assert _plan(tmp_path, root)["tests"] == reused
    assert len(calls) == (3 + legs if reused else 3)
    assert (
        "Stopped at pull request run 101: its records would pass 100 GitHub requests" in capsys.readouterr().out
    ) == (not reused)


def test_the_request_budget_drops_whole_runs_it_cannot_read(tmp_path, monkeypatch, repo, capsys):
    root, commits = repo
    newer, older = _run(102), _run(101)
    calls, _ = _github(
        monkeypatch,
        [newer, older],
        {
            102: [_record(newer, commits["record"], passed=["test_h"], leg=leg) for leg in ("1", "2")],
            101: [_record(older, commits["record"], failed=["test_h"], leg=leg) for leg in ("1", "2")],
        },
    )
    monkeypatch.setattr(ci_branch_plan, "REQUESTS", 7)
    plan = _plan(tmp_path, root)
    assert [entry["run"] for entry in plan["runs"]] == [102]
    assert sorted(_downloads(calls)) == [f"{ACTIONS}/artifacts/{n}/zip" for n in (1, 2)]
    assert "Stopped at pull request run 101: its records would pass 7 GitHub requests" in capsys.readouterr().out


def test_a_graded_record_keeps_only_what_the_choice_reads(tmp_path, monkeypatch, repo):
    root, commits = repo
    newer, older = _run(102), _run(101)
    _github(
        monkeypatch,
        [newer, older],
        {
            102: [_record(newer, commits["record"], passed=["test_h"], leg=leg) for leg in ("1", "2")],
            101: [_record(older, commits["record"], passed=["test_g"], leg=leg) for leg in ("1", "2")],
        },
    )
    kept, choose = [], ci_branch_plan.choose
    monkeypatch.setattr(ci_branch_plan, "choose", lambda found, *rest: kept.extend(found) or choose(found, *rest))
    assert _plan(tmp_path, root)["tests"] == _ids("test_h")
    assert len(kept) == 4
    assert all(set(record) == {"run", "attempt", "commit", "head"} for record, _, _ in kept)


@pytest.mark.parametrize(
    "missing",
    ["--repository", "--pull", "--head-ref", "--head-repo", "--run", "--created", "--environment", "--root", "--out"],
)
def test_every_required_flag_is_required(tmp_path, repo, missing):
    root, _ = repo
    args = _args(tmp_path, root)
    index = args.index(missing)
    with pytest.raises(SystemExit):
        ci_branch_plan.main(args[:index] + args[index + 2 :])
