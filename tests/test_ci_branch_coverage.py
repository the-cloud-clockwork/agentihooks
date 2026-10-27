import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from coverage import CoverageData

from scripts import ci_branch_coverage, ci_test_plan

pytestmark = pytest.mark.unit

REPOSITORY = "the-cloud-clockwork/agentihooks"
OLD = "def kept():\n    return 1\n\n\ndef changed():\n    return 2\n"
NEW = "import os\n\n\ndef kept():\n    return 1\n\n\ndef changed():\n    return 3\n"
REUSED = "tests/test_a.py::reused"
RAN = "tests/test_a.py::ran"


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "hooks").mkdir(parents=True)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (repo / "hooks" / "a.py").write_text(OLD)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "first head"], check=True)
    commit = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    (repo / "hooks" / "a.py").write_text(NEW)
    subprocess.run([*git, "commit", "-qam", "this head"], check=True)
    return repo, commit


def _coverage(tmp_path, reused=((-1, 2), (2, -1)), name="recorded"):
    path = tmp_path / name / ".coverage"
    path.parent.mkdir(exist_ok=True)
    data = CoverageData(basename=str(path))
    for context, arcs in {
        "": [(-1, 1), (1, -1)],
        f"{REUSED}@grp|run": list(reused),
        f"{RAN}|run": [(-5, 5), (5, -5)],
    }.items():
        data.set_context(context)
        data.add_arcs({"hooks/a.py": arcs})
    data.write()
    return path.read_bytes()


def _artifact(database, captures=True):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(".coverage", database)
        if captures:
            for index, (test, source) in enumerate(((REUSED, "kept"), (RAN, "ran")), 1):
                capture = {"result": [{"source": source}], "test": f"{test} (call)"}
                archive.writestr(f"js-coverage/capture-browser-{index}.json", json.dumps(capture))
                archive.writestr(f"js-coverage/sources/{source}.js", source)
    return buffer.getvalue()


def _github(monkeypatch, artifacts):
    def api(endpoint):
        if "/artifacts?" in endpoint:
            run = int(endpoint.split("/runs/")[1].split("/")[0])
            listed = [{"id": ident, "name": name, "expired": expired} for ident, name, _, expired in artifacts[run]]
            return json.dumps({"artifacts": listed}).encode()
        ident = int(endpoint.split("/artifacts/")[1].split("/")[0])
        return next(data for items in artifacts.values() for found, _, data, _ in items if found == ident)

    monkeypatch.setattr(ci_branch_coverage, "_api", api)


def _carry(tmp_path, repo, commit, tests=(REUSED,), branch=(REUSED,), attempt=1):
    plan = tmp_path / "reused-branch.json"
    run = {"run": 5, "attempt": attempt, "commit": commit, "head": "d" * 40, "tests": [REUSED, RAN]}
    plan.write_text(json.dumps({"version": 1, "runs": [run], "tests": list(tests)}))
    deselected = tmp_path / "reused.json"
    deselected.write_text(json.dumps({"tests": list(tests), "files": [], "branch": list(branch)}))
    out = tmp_path / ".coverage-shards"
    argv = ["--repository", REPOSITORY, "--plan", str(plan), "--deselected", str(deselected)]
    return ci_branch_coverage.main([*argv, "--repo", str(repo), "--out", str(out)]), out


def _arcs(path: Path) -> dict[str, set[tuple[int, int]]]:
    data = CoverageData(basename=str(path))
    data.read()
    return {name: set(data.arcs(name)) for name in data.measured_files()}


def test_only_tests_reused_from_a_run_and_skipped_here_are_carried():
    plan = {
        "runs": [
            {"run": 5, "attempt": 1, "commit": "a" * 40, "tests": ["t::x", "t::y", "t::z"]},
            {"run": 6, "attempt": 2, "commit": "b" * 64, "tests": ["t::w"]},
            {"run": 7, "attempt": 1, "commit": "c" * 40, "tests": ["t::y"]},
        ],
        "tests": ["t::x", "t::y", "t::w"],
    }
    deselected = {"tests": ["t::x", "t::w", "t::d"], "files": [], "branch": ["t::x", "t::w", "t::q"]}
    assert ci_branch_coverage.contributions(plan, deselected) == {
        (5, 1): ("a" * 40, {"t::x"}),
        (6, 2): ("b" * 64, {"t::w"}),
    }


def test_a_deselect_without_pull_request_tests_carries_nothing():
    plan = {"runs": [{"run": 5, "attempt": 1, "commit": "a" * 40, "tests": ["t::x"]}], "tests": ["t::x"]}
    assert ci_branch_coverage.contributions(plan, {"tests": ["t::x"], "files": []}) == {}


def test_the_carry_reads_github_through_the_planner_helper_that_retries_server_errors():
    assert ci_branch_coverage._api is ci_test_plan._api


@pytest.mark.parametrize(
    "runs",
    [
        [{"run": "5", "attempt": 1, "commit": "a" * 40, "tests": []}],
        [{"run": 5, "commit": "a" * 40, "tests": []}],
        [{"run": 5, "attempt": 1, "commit": "--upload-pack=x", "tests": []}],
        [
            {"run": 5, "attempt": 1, "commit": "a" * 40, "tests": []},
            {"run": 5, "attempt": 1, "commit": "b" * 40, "tests": []},
        ],
    ],
)
def test_a_malformed_plan_is_unusable(runs):
    with pytest.raises(ci_branch_coverage.Unusable):
        ci_branch_coverage.contributions({"runs": runs, "tests": []}, {"branch": []})


def test_the_reused_tests_coverage_and_captures_are_carried_onto_this_heads_lines(tmp_path, monkeypatch, capsys):
    repo, commit = _repo(tmp_path)
    _github(monkeypatch, {5: [(11, "branch-coverage-3.12-2-1", _artifact(_coverage(tmp_path)), False)]})
    status, out = _carry(tmp_path, repo, commit)
    assert status == 0
    carried = out / "branch-5" / "2-1"
    assert _arcs(carried / ".coverage") == {"hooks/a.py": {(-4, 4), (4, -4), (-4, 5), (5, -4)}}
    assert sorted(path.name for path in (carried / "js-coverage").glob("capture-*.json")) == ["capture-browser-1.json"]
    assert sorted(path.name for path in (carried / "js-coverage" / "sources").iterdir()) == ["kept.js"]
    assert (
        f"Pull request run 5 attempt 1 on {commit}: kept the coverage of 1 of 1 reused tests and 1 page captures"
        " from 1 of 1 artifacts; 1 changed files remapped"
    ) in capsys.readouterr().out


def test_only_the_planned_attempts_coverage_is_carried_from_a_rerun(tmp_path, monkeypatch, capsys):
    repo, commit = _repo(tmp_path)
    failed = _artifact(_coverage(tmp_path, reused=((-5, 5), (5, -5)), name="failed"), captures=False)
    passed = _artifact(_coverage(tmp_path, name="passed"))
    artifacts = [(11, "branch-coverage-3.12-2-1", failed, False), (12, "branch-coverage-3.12-2-2", passed, False)]
    _github(monkeypatch, {5: artifacts})
    status, out = _carry(tmp_path, repo, commit, attempt=2)
    assert status == 0
    assert sorted(path.name for path in (out / "branch-5").iterdir()) == ["2-2"]
    assert _arcs(out / "branch-5" / "2-2" / ".coverage") == {"hooks/a.py": {(-4, 4), (4, -4), (-4, 5), (5, -4)}}
    assert "from 1 of 1 artifacts" in capsys.readouterr().out


def test_a_test_reused_from_the_run_but_not_skipped_here_keeps_no_coverage(tmp_path, monkeypatch):
    repo, commit = _repo(tmp_path)
    _github(monkeypatch, {5: [(11, "branch-coverage-3.12-2-1", _artifact(_coverage(tmp_path)), False)]})
    status, out = _carry(tmp_path, repo, commit, tests=(REUSED, RAN), branch=(REUSED,))
    assert status == 0
    assert _arcs(out / "branch-5" / "2-1" / ".coverage") == {"hooks/a.py": {(-4, 4), (4, -4), (-4, 5), (5, -4)}}


def test_a_run_without_coverage_artifacts_is_warned_and_carries_nothing(tmp_path, monkeypatch, capsys):
    repo, commit = _repo(tmp_path)
    data = _artifact(_coverage(tmp_path))
    _github(monkeypatch, {5: [(11, "branch-coverage-3.12-2-1", data, True), (12, "coverage-3.12-2", data, False)]})
    status, out = _carry(tmp_path, repo, commit)
    assert status == 0
    assert not (out / "branch-5").exists()
    assert (
        "::warning title=Unusable branch coverage::Pull request run 5 attempt 1: it kept no unexpired"
        in capsys.readouterr().out
    )


def test_an_unusable_artifact_is_warned_and_the_others_still_carry(tmp_path, monkeypatch, capsys):
    repo, commit = _repo(tmp_path)
    database = _coverage(tmp_path)
    artifacts = [
        (11, "branch-coverage-3.12-2-1", _artifact(database), False),
        (12, "branch-coverage-3.12-3-1", _artifact(database[: len(database) // 2], captures=False), False),
    ]
    _github(monkeypatch, {5: artifacts})
    status, out = _carry(tmp_path, repo, commit)
    output = capsys.readouterr().out
    assert status == 0
    assert (out / "branch-5" / "2-1" / ".coverage").is_file()
    assert not (out / "branch-5" / "3-1").exists()
    assert (
        "::warning title=Unusable branch coverage::Pull request run 5 attempt 1: branch-coverage-3.12-3-1: .coverage"
        in output
    )
    assert "from 1 of 2 artifacts" in output


def test_a_commit_that_cannot_be_fetched_skips_its_run(tmp_path, monkeypatch, capsys):
    repo, _ = _repo(tmp_path)

    def api(endpoint):
        raise AssertionError(endpoint)

    monkeypatch.setattr(ci_branch_coverage, "_api", api)
    status, out = _carry(tmp_path, repo, "e" * 40)
    assert status == 0
    assert not out.exists()
    assert "::warning title=Unusable branch coverage::Pull request run 5 attempt 1:" in capsys.readouterr().out


def test_a_malformed_plan_warns_and_passes(tmp_path, capsys):
    plan, deselected = tmp_path / "reused-branch.json", tmp_path / "reused.json"
    plan.write_text(json.dumps({"runs": "5", "tests": []}))
    deselected.write_text(json.dumps({"tests": [], "files": [], "branch": []}))
    argv = ["--repository", REPOSITORY, "--plan", str(plan), "--deselected", str(deselected)]
    assert ci_branch_coverage.main([*argv, "--out", str(tmp_path / "out")]) == 0
    assert "::warning title=Unusable branch plan::" in capsys.readouterr().out


def test_no_plan_carries_nothing(tmp_path, capsys):
    argv = ["--repository", REPOSITORY, "--plan", str(tmp_path / "none"), "--deselected", str(tmp_path / "none")]
    assert ci_branch_coverage.main([*argv, "--out", str(tmp_path / "out")]) == 0
    assert capsys.readouterr().out == "No tests reused from pull request runs; no coverage to carry.\n"
