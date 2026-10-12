import io
import json
import subprocess
import zipfile

import pytest

from scripts.ci_mutation import reuse

GREEN = {"files": [{"path": "hooks/a.py", "failures": []}], "not_mutated": [], "failed": False}
RED = {"files": [{"path": "hooks/a.py", "failures": [{"name": "m1"}]}], "not_mutated": [], "failed": True}
REDS = [
    RED,
    {**GREEN, "failed": True},
    {**GREEN, "not_mutated": [{"path": "hooks/a.py", "reason": "no tests"}]},
    {**GREEN, "files": [{"path": "hooks/a.py", "failures": [{"name": "m1"}]}]},
    {**GREEN, "files": [{"path": "hooks/a.py", "failures": []}, {"path": "hooks/b.py", "failures": [{"name": "m"}]}]},
]
RUNS = "actions/workflows/mutation-preflight.yml/runs?event=push&status=completed&per_page=30&"
SOURCE = {"hooks/a.py": "def f():\n    return 1\n", "tests/test_a.py": "def test_one():\n    pass\n"}
ADDED = {"tests/test_a.py": "def test_one():\n    pass\n\n\ndef test_two():\n    pass\n"}


def _archive(report):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as zipped:
        zipped.writestr(zipfile.ZipInfo("report.json"), json.dumps(report))
        zipped.writestr(zipfile.ZipInfo("scope.json"), json.dumps({"base": "old", "head": "old"}))
        zipped.writestr(zipfile.ZipInfo("mutants.log"), "graded\n")
    return data.getvalue()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path).decode().strip()

    def commit(files):
        for name, text in files.items():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            git("add", name)
        git("commit", "-q", "-m", "change")
        return git("rev-parse", "HEAD")

    git("init", "-q", "-b", "branch")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    monkeypatch.chdir(tmp_path)
    return tmp_path, commit, git


def _github(monkeypatch, head, branch_runs, head_runs, reports, branch="branch"):
    calls = []
    answers = {
        f"{RUNS}head_sha={head}": {"workflow_runs": head_runs},
        f"{RUNS}branch={branch}": {"workflow_runs": branch_runs},
    }
    for run, graded in reports.items():
        artifact = {"id": run, "name": "mutation-preflight-report", "expired": False}
        answers[f"actions/runs/{run}/artifacts?per_page=100"] = {"artifacts": [artifact]}
        answers[f"actions/artifacts/{run}/zip"] = _archive(graded)

    def api(path, binary=False):
        calls.append(path)
        assert binary is path.endswith("/zip")
        return answers.get(path, {"artifacts": []})

    monkeypatch.setattr(reuse, "api", api)
    return calls


def _run(number, head, conclusion="success", branch="branch"):
    return {"id": number, "head_sha": head, "head_branch": branch, "conclusion": conclusion}


def _main(monkeypatch, tmp_path, branch="branch"):
    output = tmp_path.parent / f"{tmp_path.name}-output"
    output.write_text("open=false\n")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr("sys.argv", ["reuse", branch, "99"])
    assert reuse.main() == 0
    written = output.read_text()
    assert written.startswith("open=false\n")
    return written.removeprefix("open=false\n")


def test_a_head_that_only_adds_tests_since_the_latest_green_report_reuses_it(repo, monkeypatch, capsys):
    root, commit, _ = repo
    graded = commit(SOURCE)
    head = commit(ADDED)
    (root / ".mutation-gate").mkdir()
    (root / ".mutation-gate/scope.json").write_text('{"base": "new", "head": "new"}')
    _github(monkeypatch, head, [_run(5, graded)], [], {5: GREEN})
    assert _main(monkeypatch, root) == "reused=true\n"
    assert capsys.readouterr().out == f"Reused the green mutation report of run 5 for {graded}\n"
    assert json.loads((root / ".mutation-gate/report.json").read_text()) == GREEN
    assert (root / ".mutation-gate/mutants.log").read_text() == "graded\n"
    assert json.loads((root / ".mutation-gate/scope.json").read_text()) == {"base": "new", "head": "new"}
    assert (root / ".mutation-gate/reuse.json").read_text() == json.dumps({"run": 5, "head": graded}) + "\n"


def test_a_head_already_graded_green_on_another_branch_reuses_that_report(repo, monkeypatch):
    root, commit, _ = repo
    head = commit(SOURCE)
    _github(monkeypatch, head, [], [_run(8, head, branch="other"), _run(7, head, branch="other")], {7: GREEN})
    assert _main(monkeypatch, root) == "reused=true\n"
    assert json.loads((root / ".mutation-gate/reuse.json").read_text())["run"] == 7


def test_a_newer_red_report_on_the_same_head_blocks_an_older_green(repo, monkeypatch):
    root, commit, _ = repo
    head = commit(SOURCE)
    _github(monkeypatch, head, [_run(7, head)], [_run(8, head, "failure"), _run(7, head)], {8: RED, 7: GREEN})
    assert _main(monkeypatch, root) == "reused=false\n"


@pytest.mark.parametrize("same_head", [True, False])
def test_a_newer_failed_run_without_a_report_blocks_an_older_green(repo, monkeypatch, same_head):
    root, commit, _ = repo
    graded = commit(SOURCE)
    head = graded if same_head else commit(ADDED)
    runs = [_run(8, graded, "failure"), _run(7, graded)]
    _github(monkeypatch, head, runs, runs if same_head else [], {7: GREEN})
    assert _main(monkeypatch, root) == "reused=false\n"


@pytest.mark.parametrize("red", REDS)
def test_a_red_report_on_the_same_head_is_never_reused(repo, monkeypatch, red):
    root, commit, _ = repo
    head = commit(SOURCE)
    _github(monkeypatch, head, [_run(7, head, "failure")], [_run(7, head, "failure", branch="other")], {7: red})
    assert _main(monkeypatch, root) == "reused=false\n"


@pytest.mark.parametrize("same_head", [True, False])
def test_a_green_report_from_a_failed_run_is_never_reused(repo, monkeypatch, same_head):
    root, commit, _ = repo
    graded = commit(SOURCE)
    head = graded if same_head else commit(ADDED)
    failed = [_run(5, graded, "failure")]
    _github(monkeypatch, head, failed, failed if same_head else [], {5: GREEN})
    assert _main(monkeypatch, root) == "reused=false\n"


@pytest.mark.parametrize(
    "change",
    [
        {"hooks/a.py": "def f():\n    return 2\n"},
        {"tests/test_a.py": "def test_one():\n    assert False\n"},
        {"tests/conftest.py": "import pytest\n"},
        {"tests/data.json": "{}\n"},
        {**ADDED, "uv.lock": "changed\n"},
        {**ADDED, "tests.py": "x = 1\n"},
    ],
)
def test_a_source_change_or_an_edited_test_since_the_green_report_mutates_again(repo, monkeypatch, capsys, change):
    root, commit, _ = repo
    graded = commit(SOURCE)
    head = commit(change)
    _github(monkeypatch, head, [_run(5, graded)], [], {5: GREEN})
    assert _main(monkeypatch, root) == "reused=false\n"
    assert capsys.readouterr().out == "No green mutation report covers this head; mutating\n"
    assert not (root / ".mutation-gate").exists()


def test_moving_a_source_file_into_the_tests_folder_is_a_source_change(repo, monkeypatch):
    root, commit, git = repo
    graded = commit({"hooks/test_b.py": "def test_b():\n    pass\n"})
    (root / "tests").mkdir()
    git("mv", "hooks/test_b.py", "tests/test_b.py")
    git("commit", "-q", "-m", "move")
    head = git("rev-parse", "HEAD")
    _github(monkeypatch, head, [_run(5, graded)], [], {5: GREEN})
    assert _main(monkeypatch, root) == "reused=false\n"


def test_a_green_head_outside_the_branch_history_is_never_reused(repo, monkeypatch):
    root, commit, git = repo
    base = commit({"hooks/a.py": "def f():\n    return 1\n"})
    git("checkout", "-q", "-b", "side")
    graded = commit({"tests/test_a.py": "def test_one():\n    pass\n"})
    git("checkout", "-q", "branch")
    head = commit(ADDED)
    assert base != graded
    _github(monkeypatch, head, [_run(5, graded)], [], {5: GREEN})
    assert _main(monkeypatch, root) == "reused=false\n"


def test_only_the_latest_graded_run_of_the_branch_counts_so_a_red_one_blocks_an_older_green(repo, monkeypatch):
    root, commit, _ = repo
    green = commit({"hooks/a.py": "def f():\n    return 1\n"})
    red = commit({"tests/test_a.py": "def test_one():\n    pass\n"})
    head = commit({"tests/test_b.py": "def test_two():\n    pass\n"})
    runs = [_run(8, red, "failure"), _run(6, red, "cancelled"), _run(5, green)]
    _github(monkeypatch, head, runs, [], {8: RED, 6: GREEN, 5: GREEN})
    assert _main(monkeypatch, root) == "reused=false\n"


def test_runs_without_a_report_or_a_grading_conclusion_are_passed_over(repo, monkeypatch):
    root, commit, _ = repo
    green = commit({"hooks/a.py": "def f():\n    return 1\n"})
    head = commit({"tests/test_a.py": "def test_one():\n    pass\n"})
    runs = [_run(9, green), _run(6, green, "cancelled"), _run(5, green)]
    _github(monkeypatch, head, runs, [], {6: RED, 5: GREEN})
    assert _main(monkeypatch, root) == "reused=true\n"


def test_the_current_run_and_unknown_heads_never_count(repo, monkeypatch):
    root, commit, _ = repo
    head = commit(SOURCE)
    _github(monkeypatch, head, [_run(99, head), _run(5, "0" * 40)], [_run(99, head)], {99: GREEN, 5: GREEN})
    assert _main(monkeypatch, root) == "reused=false\n"


def test_the_branch_name_is_quoted_into_the_run_query(repo, monkeypatch):
    root, commit, _ = repo
    graded = commit(SOURCE)
    head = commit(ADDED)
    _github(monkeypatch, head, [_run(5, graded)], [], {5: GREEN}, branch="a%26b")
    assert _main(monkeypatch, root, branch="a&b") == "reused=true\n"


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, ["gh", "api"]),
        OSError("gh missing"),
        ValueError("bad json"),
        KeyError("workflow_runs"),
        zipfile.BadZipFile("bad zip"),
    ],
)
def test_an_unreadable_lookup_mutates_instead_of_failing(repo, monkeypatch, capsys, error):
    root, commit, _ = repo
    commit(SOURCE)

    def api(path, binary=False):
        raise error

    monkeypatch.setattr(reuse, "api", api)
    assert _main(monkeypatch, root) == "reused=false\n"
    assert capsys.readouterr().out == (
        f"Reuse lookup failed, mutating instead: {error}\nNo green mutation report covers this head; mutating\n"
    )


def test_the_lookup_reads_completed_push_runs_by_head_then_by_branch(repo, monkeypatch):
    root, commit, _ = repo
    head = commit(SOURCE)
    calls = _github(monkeypatch, head, [], [], {})
    _main(monkeypatch, root)
    assert calls == [f"{RUNS}head_sha={head}", f"{RUNS}branch=branch"]


def test_the_newest_unexpired_report_artifact_is_read(monkeypatch):
    artifacts = [
        {"id": 2, "name": "mutation-preflight-report", "expired": False},
        {"id": 4, "name": "mutation-preflight-report", "expired": True},
        {"id": 5, "name": "other", "expired": False},
        {"id": 3, "name": "mutation-preflight-report", "expired": False},
    ]
    answers = {
        "actions/runs/1/artifacts?per_page=100": {"artifacts": artifacts},
        "actions/artifacts/3/zip": _archive(GREEN),
    }
    monkeypatch.setattr(reuse, "api", lambda path, binary=False: answers[path])
    assert reuse.report(1) == (_archive(GREEN), True)
    answers["actions/artifacts/3/zip"] = _archive(REDS[-1])
    assert reuse.report(1) == (_archive(REDS[-1]), False)
    answers["actions/runs/1/artifacts?per_page=100"] = {"artifacts": artifacts[1:3]}
    assert reuse.report(1) is None


@pytest.mark.parametrize("red", REDS)
def test_a_report_with_any_red_mark_reads_red(monkeypatch, red):
    answers = {
        "actions/runs/1/artifacts?per_page=100": {
            "artifacts": [{"id": 3, "name": "mutation-preflight-report", "expired": False}]
        },
        "actions/artifacts/3/zip": _archive(red),
    }
    monkeypatch.setattr(reuse, "api", lambda path, binary=False: answers[path])
    assert reuse.report(1) == (_archive(red), False)


def test_the_api_reads_the_repository_through_gh(monkeypatch):
    seen = []

    def run(command, **options):
        seen.append((command, options))
        return subprocess.CompletedProcess(command, 0, b'{"a": 1}', b"")

    monkeypatch.setattr(reuse.subprocess, "run", run)
    assert reuse.api("x") == {"a": 1}
    assert reuse.api("y", binary=True) == b'{"a": 1}'
    assert seen == [
        (["gh", "api", "repos/{owner}/{repo}/x"], {"capture_output": True, "check": True}),
        (["gh", "api", "repos/{owner}/{repo}/y"], {"capture_output": True, "check": True}),
    ]


def test_a_reused_report_never_brings_the_source_scope(repo, monkeypatch):
    root, commit, _ = repo
    head = commit(SOURCE)
    _github(monkeypatch, head, [], [_run(7, head, branch="other")], {7: GREEN})
    assert _main(monkeypatch, root) == "reused=true\n"
    names = sorted(path.name for path in (root / ".mutation-gate").iterdir())
    assert names == ["mutants.log", "report.json", "reuse.json"]
