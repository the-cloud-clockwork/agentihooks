import io
import json
import re
import subprocess
import threading
import zipfile

import pytest
from coverage import CoverageData

from scripts import ci_branch_coverage, ci_test_plan
from tests import coverage_check

pytestmark = pytest.mark.unit

REPOSITORY = "the-cloud-clockwork/agentihooks"
OLD = "def kept():\n    return 1\n\n\ndef changed():\n    return 2\n"
NEW = "import os\n\n\ndef kept():\n    return 1\n\n\ndef changed():\n    return 3\n"
REUSED = "tests/test_a.py::reused"
OTHER = "tests/test_a.py::other"
RAN = "tests/test_a.py::ran"
RECORDED = {"": [(-1, 1), (1, -1)], f"{REUSED}@grp|run": [(-1, 2), (2, -1)], f"{RAN}|run": [(-5, 5), (5, -5)]}
CARRIED = {"hooks/a.py": {(-4, 4), (4, -4), (-4, 5), (5, -4)}}
WARNING = "::warning title=Unusable branch coverage::Pull request run 5 attempt 1: "
NOT_CARRIED = "; that coverage is not carried."
LISTING = f"repos/{REPOSITORY}/actions/runs/5/artifacts?per_page=100&page="
DOWNLOAD = f"repos/{REPOSITORY}/actions/artifacts/"
RUN = {"run": 5, "attempt": 1, "commit": "a" * 40, "tests": []}


def _git(repo, *args):
    command = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args]
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path):
    origin = tmp_path / "origin"
    (origin / "hooks").mkdir(parents=True)
    _git(origin, "init", "-q")
    _git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
    (origin / "hooks" / "a.py").write_text(OLD)
    _git(origin, "add", ".")
    _git(origin, "commit", "-qm", "first head")
    commit = _git(origin, "rev-parse", "HEAD")
    (origin / "hooks" / "a.py").write_text(NEW)
    _git(origin, "commit", "-qam", "this head")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--depth", "1", origin.as_uri(), str(clone)], check=True)
    return clone, commit


def _database(tmp_path, contexts=RECORDED):
    path = tmp_path / f"db-{len(list(tmp_path.glob('db-*')))}" / ".coverage"
    path.parent.mkdir()
    data = CoverageData(basename=str(path))
    for context, arcs in contexts.items():
        data.set_context(context)
        data.add_arcs({"hooks/a.py": arcs})
    data.write()
    return path.read_bytes()


def _archive(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _captured(*tests):
    files = {}
    for index, test in enumerate(tests, 1):
        capture = {"result": [{"source": f"s{index}"}], "test": f"{test} (call)"}
        files[f"js-coverage/capture-browser-{index}.json"] = json.dumps(capture)
        files[f"js-coverage/sources/s{index}.js"] = test
    return files


def _item(ident, name, expired=False):
    return {"id": ident, "name": name, "expired": expired}


def _github(monkeypatch, pages, archives):
    def api(endpoint):
        if endpoint.startswith(LISTING):
            return json.dumps({"artifacts": pages.get(int(endpoint.removeprefix(LISTING)), [])}).encode()
        assert endpoint.startswith(DOWNLOAD) and endpoint.endswith("/zip"), endpoint
        return archives[int(endpoint.removeprefix(DOWNLOAD).removesuffix("/zip"))]

    monkeypatch.setattr(ci_branch_coverage, "_api", api)


def _carry(tmp_path, repo, commit, attempt=1, tests=(REUSED, OTHER), branch=(REUSED, OTHER)):
    plan = tmp_path / "reused-branch.json"
    run = {"run": 5, "attempt": attempt, "commit": commit, "head": "d" * 40, "tests": [REUSED, OTHER, RAN]}
    plan.write_text(json.dumps({"version": 1, "runs": [run], "tests": list(tests)}))
    deselected = tmp_path / "reused.json"
    deselected.write_text(json.dumps({"tests": list(tests), "files": [], "branch": list(branch)}))
    out = tmp_path / ".coverage-shards"
    argv = ["--repository", REPOSITORY, "--plan", str(plan), "--deselected", str(deselected), "--repo", str(repo)]
    return ci_branch_coverage.main([*argv, "--out", str(out)]), out


def _arcs(path):
    data = CoverageData(basename=str(path))
    data.read()
    return {name: set(data.arcs(name)) for name in data.measured_files()}


def _summary(commit, captures, used, listed, attempt=1):
    return (
        f"Pull request run 5 attempt {attempt} on {commit}: 2 reused tests, {captures} page captures carried"
        f" from {used} of {listed} artifacts; 1 changed files remapped"
    )


def _legs(out):
    return sorted(path.name for path in (out / "branch-5").iterdir())


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
    plan = {"runs": [{**RUN, "tests": ["t::x"]}], "tests": ["t::x"]}
    assert ci_branch_coverage.contributions(plan, {"tests": ["t::x"], "files": []}) == {}


@pytest.mark.parametrize(
    ("plan", "deselected", "message"),
    [
        ("x", {}, "the branch plan has no runs and tests lists"),
        ({"runs": "5", "tests": []}, {}, "the branch plan has no runs and tests lists"),
        ({"runs": [], "tests": "x"}, {}, "the branch plan has no runs and tests lists"),
        ({"runs": [], "tests": []}, "x", "the deselected tests hold no valid branch list"),
        ({"runs": [], "tests": []}, {"branch": "x"}, "the deselected tests hold no valid branch list"),
        ({"runs": [{**RUN, "run": "5"}], "tests": []}, {}, "the branch plan holds a malformed run"),
        (
            {"runs": [{"run": 5, "commit": "a" * 40, "tests": []}], "tests": []},
            {},
            "the branch plan holds a malformed run",
        ),
        ({"runs": [{**RUN, "commit": "--upload-pack=x"}], "tests": []}, {}, "the branch plan holds a malformed run"),
        ({"runs": [RUN, {**RUN, "commit": "b" * 40}], "tests": []}, {}, "pull request run 5 names two commits"),
    ],
)
def test_a_malformed_plan_is_unusable_and_named(plan, deselected, message):
    with pytest.raises(ci_branch_coverage.Unusable, match=f"^{re.escape(message)}$"):
        ci_branch_coverage.contributions(plan, deselected)


def test_the_carry_reads_github_through_the_planner_helper_that_retries_server_errors():
    assert ci_branch_coverage._api is ci_test_plan._api


def test_the_reused_tests_coverage_and_captures_are_carried_onto_this_heads_lines(tmp_path, monkeypatch, capsys):
    repo, commit = _repo(tmp_path)
    archive = _archive({".coverage": _database(tmp_path), **_captured(REUSED, RAN)})
    _github(monkeypatch, {1: [_item(11, "branch-coverage-3.12-2-1")]}, {11: archive})
    status, out = _carry(tmp_path, repo, commit)
    output = capsys.readouterr()
    assert status == 0
    carried = out / "branch-5" / "2-1"
    assert _arcs(carried / ".coverage") == CARRIED
    assert sorted(path.name for path in (carried / "js-coverage").glob("capture-*.json")) == ["capture-browser-1.json"]
    assert sorted(path.name for path in (carried / "js-coverage" / "sources").iterdir()) == ["s1.js"]
    assert output.out == _summary(commit, 1, 1, 1) + "\n"
    assert "The branch coverage carry made " in output.err


def test_only_the_planned_attempts_coverage_is_carried_from_a_rerun(tmp_path, monkeypatch, capsys):
    repo, commit = _repo(tmp_path)
    failed = _archive({".coverage": _database(tmp_path, {f"{REUSED}|run": [(-5, 5), (5, -5)]})})
    passed = _archive({".coverage": _database(tmp_path)})
    items = [_item(11, "branch-coverage-3.12-2-1"), _item(12, "branch-coverage-3.12-2-2")]
    _github(monkeypatch, {1: items}, {11: failed, 12: passed})
    status, out = _carry(tmp_path, repo, commit, attempt=2)
    assert status == 0
    assert _legs(out) == ["2-2"]
    assert _arcs(out / "branch-5" / "2-2" / ".coverage") == CARRIED
    assert capsys.readouterr().out == _summary(commit, 0, 1, 1, attempt=2) + "\n"


def test_a_test_reused_from_the_run_but_not_skipped_here_keeps_no_coverage(tmp_path, monkeypatch):
    repo, commit = _repo(tmp_path)
    _github(
        monkeypatch, {1: [_item(11, "branch-coverage-3.12-2-1")]}, {11: _archive({".coverage": _database(tmp_path)})}
    )
    status, out = _carry(tmp_path, repo, commit, tests=(REUSED, RAN), branch=(REUSED,))
    assert status == 0
    assert _arcs(out / "branch-5" / "2-1" / ".coverage") == CARRIED


def test_artifacts_on_every_listed_page_are_carried_for_the_planned_attempt_only(tmp_path, monkeypatch, capsys):
    repo, commit = _repo(tmp_path)
    archive = _archive({".coverage": _database(tmp_path)})
    fillers = [_item(100 + index, f"durations-3.12-{index}") for index in range(99)]
    pages = {
        1: [*fillers, _item(11, "branch-coverage-3.12-1-1")],
        2: [
            _item(12, "branch-coverage-3.12-2-1"),
            _item(13, "branch-coverage-3.12-3-1", expired=True),
            _item(14, "branch-coverage-3.12-4-2"),
            _item(15, "branch-coverage-3.12-x-1"),
        ],
    }
    _github(monkeypatch, pages, {11: archive, 12: archive})
    status, out = _carry(tmp_path, repo, commit)
    assert status == 0
    assert _legs(out) == ["1-1", "2-1"]
    assert capsys.readouterr().out == _summary(commit, 0, 2, 2) + "\n"


def test_each_unusable_artifact_is_named_and_the_usable_ones_add_up(tmp_path, monkeypatch, capsys):
    repo, commit = _repo(tmp_path)
    database = _database(tmp_path)
    broken = tmp_path / "broken.coverage"
    broken.write_bytes(database[: len(database) // 2])
    other = _database(tmp_path, {f"{OTHER}|run": [(-1, 2), (2, -1)]})
    names = [f"branch-coverage-3.12-{leg}-1" for leg in range(1, 6)]
    archives = {
        1: b"not a zip",
        2: _archive(_captured(REUSED)),
        3: _archive({".coverage": broken.read_bytes()}),
        4: _archive({".coverage": database, **_captured(REUSED)}),
        5: _archive({".coverage.w1": database, ".coverage.w2": other, **_captured(OTHER, RAN)}),
    }
    _github(monkeypatch, {1: [_item(ident, name) for ident, name in enumerate(names, 1)]}, archives)
    status, out = _carry(tmp_path, repo, commit)
    assert status == 0
    assert capsys.readouterr().out.splitlines() == [
        f"{WARNING}{names[0]}: not a readable archive (File is not a zip file){NOT_CARRIED}",
        f"{WARNING}{names[1]}: no coverage data{NOT_CARRIED}",
        f"{WARNING}{names[2]}: .coverage is unusable ({coverage_check.problem(broken)}){NOT_CARRIED}",
        _summary(commit, 2, 2, 5),
    ]
    assert _legs(out) == ["4-1", "5-1"]
    assert _arcs(out / "branch-5" / "5-1" / ".coverage.w1") == CARRIED
    assert _arcs(out / "branch-5" / "5-1" / ".coverage.w2") == {"hooks/a.py": {(-4, 5), (5, -4)}}


def test_a_run_without_coverage_artifacts_is_warned_and_carries_nothing(tmp_path, monkeypatch, capsys):
    repo, commit = _repo(tmp_path)
    items = [_item(11, "branch-coverage-3.12-2-1", expired=True), _item(12, "coverage-3.12-2")]
    _github(monkeypatch, {1: items}, {})
    status, out = _carry(tmp_path, repo, commit)
    assert status == 0
    assert not out.exists()
    assert capsys.readouterr().out == f"{WARNING}it kept no unexpired branch-coverage-3.12 artifact{NOT_CARRIED}\n"


def test_a_commit_that_cannot_be_fetched_skips_its_run(tmp_path, monkeypatch, capsys):
    repo, _ = _repo(tmp_path)
    monkeypatch.setattr(ci_branch_coverage, "_api", lambda endpoint: pytest.fail(endpoint))
    status, out = _carry(tmp_path, repo, "e" * 40)
    fetch = ["git", "-C", str(repo), "fetch", "--no-tags", "--quiet", "origin", "e" * 40]
    assert status == 0
    assert not out.exists()
    assert capsys.readouterr().out == f"{WARNING}Command '{fetch}' returned non-zero exit status 128.{NOT_CARRIED}\n"


def test_no_more_than_four_downloads_run_at_once(tmp_path, monkeypatch):
    repo, commit = _repo(tmp_path)
    archive = _archive({".coverage": _database(tmp_path)})
    items = [_item(leg, f"branch-coverage-3.12-{leg}-1") for leg in range(1, 7)]
    lock, crowded, active, peak = threading.Lock(), threading.Event(), [0], [0]

    def api(endpoint):
        if endpoint.startswith(LISTING):
            return json.dumps({"artifacts": items}).encode()
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            if active[0] > 4:
                crowded.set()
        crowded.wait(0.5)
        with lock:
            active[0] -= 1
        return archive

    monkeypatch.setattr(ci_branch_coverage, "_api", api)
    status, out = _carry(tmp_path, repo, commit)
    assert status == 0
    assert peak[0] <= 4
    assert _legs(out) == [f"{leg}-1" for leg in range(1, 7)]


def test_a_malformed_plan_warns_and_passes(tmp_path, capsys):
    plan, deselected = tmp_path / "reused-branch.json", tmp_path / "reused.json"
    plan.write_text(json.dumps({"runs": "5", "tests": []}))
    deselected.write_text(json.dumps({"tests": [], "files": [], "branch": []}))
    argv = ["--repository", REPOSITORY, "--plan", str(plan), "--deselected", str(deselected), "--repo", str(tmp_path)]
    assert ci_branch_coverage.main([*argv, "--out", str(tmp_path / "out")]) == 0
    assert capsys.readouterr().out == (
        "::warning title=Unusable branch plan::the branch plan has no runs and tests lists;"
        " no coverage is carried from pull request runs.\n"
    )


@pytest.mark.parametrize("present", [(), ("reused-branch.json",), ("reused.json",)])
def test_without_both_plans_nothing_is_carried(tmp_path, capsys, present):
    for name in present:
        (tmp_path / name).write_text(json.dumps({"runs": [], "tests": [], "branch": []}))
    argv = ["--repository", REPOSITORY, "--plan", str(tmp_path / "reused-branch.json")]
    argv += ["--deselected", str(tmp_path / "reused.json"), "--repo", str(tmp_path), "--out", str(tmp_path / "out")]
    assert ci_branch_coverage.main(argv) == 0
    assert capsys.readouterr().out == "No tests reused from pull request runs; no coverage to carry.\n"


@pytest.mark.parametrize("flag", ["--repository", "--plan", "--deselected", "--repo", "--out"])
def test_every_input_is_required(tmp_path, flag):
    values = {
        "--repository": REPOSITORY,
        "--plan": str(tmp_path / "none"),
        "--deselected": str(tmp_path / "none"),
        "--repo": str(tmp_path),
        "--out": str(tmp_path / "out"),
    }
    argv = [part for name, value in values.items() if name != flag for part in (name, value)]
    with pytest.raises(SystemExit) as raised:
        ci_branch_coverage.main(argv)
    assert raised.value.code == 2
