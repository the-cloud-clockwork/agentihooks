import json
import subprocess
from pathlib import Path

import yaml
from coverage import CoverageData

from tests import coverage_check, reused_coverage

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"
OLD = "def kept():\n    return 1\n\n\ndef changed():\n    return 2\n"
NEW = "import os\n\n\ndef kept():\n    return 1\n\n\ndef changed():\n    return 3\n"


def _record(path: Path, arcs: dict[str, dict[str, list[tuple[int, int]]]]) -> None:
    data = CoverageData(basename=str(path))
    for context, files in arcs.items():
        data.set_context(context)
        data.add_arcs(files)
    data.write()


def _arcs(path: Path) -> dict[str, set[tuple[int, int]]]:
    data = CoverageData(basename=str(path))
    data.read()
    return {name: set(data.arcs(name)) for name in data.measured_files()}


def _steps(job: str) -> dict[str, dict]:
    return {step.get("name"): step for step in yaml.safe_load(WORKFLOW.read_text())["jobs"][job]["steps"]}


def test_unchanged_source_lines_follow_their_text_and_changed_lines_have_no_target():
    lines = reused_coverage.line_map(OLD, NEW)
    assert lines[1] == 4 and lines[2] == 5 and lines[5] == 8
    assert 6 not in lines


def test_only_the_reused_tests_and_import_time_keep_their_arcs_moved_onto_the_head_lines(tmp_path):
    source = tmp_path / "dev.coverage"
    _record(
        source,
        {
            "": {"hooks/a.py": [(-1, 1), (1, -1)], "hooks/gone.py": [(-1, 1)]},
            "tests/test_a.py::test_kept@grp|run": {"hooks/a.py": [(-1, 2), (2, -1)], "hooks/b.py": [(3, 4)]},
            "tests/test_a.py::test_rerun|run": {"hooks/a.py": [(-5, 6), (6, -5)], "hooks/b.py": [(7, 8)]},
        },
    )
    out = tmp_path / "reused" / ".coverage"
    moved = {"hooks/a.py": reused_coverage.line_map(OLD, NEW), "hooks/gone.py": None}
    assert reused_coverage.keep(source, out, {"tests/test_a.py::test_kept"}, moved) == 1
    assert _arcs(out) == {"hooks/a.py": {(-4, 4), (4, -4), (-4, 5), (5, -4)}, "hooks/b.py": {(3, 4)}}


def test_reusing_every_test_with_no_change_keeps_the_dev_arcs_exactly(tmp_path):
    source = tmp_path / "dev.coverage"
    recorded = {"tests/test_a.py::one|run": {"hooks/a.py": [(-1, 2), (2, -1)]}, "": {"hooks/b.py": [(1, 2)]}}
    _record(source, recorded)
    out = tmp_path / "out.coverage"
    assert reused_coverage.keep(source, out, {"tests/test_a.py::one"}, {}) == 1
    assert _arcs(out) == _arcs(source)


def test_files_only_executed_by_changed_tests_do_not_break_the_carried_coverage_merge(tmp_path):
    from coverage import Coverage

    kept = str(tmp_path / "kept.py")
    missing = str(tmp_path / "sample.py")
    Path(kept).write_text("value = 1\n")
    for arcs in (True, False):
        source = tmp_path / f"source-{arcs}.coverage"
        data = CoverageData(basename=str(source))
        record = data.add_arcs if arcs else data.add_lines
        data.set_context("tests/test_a.py::kept|run")
        record({kept: [(-1, 1), (1, -1)] if arcs else [1]})
        data.set_context("tests/test_a.py::changed|run")
        record({missing: [(-1, 1), (1, -1)] if arcs else [1]})
        data.write()
        out = tmp_path / f"carry-{arcs}" / ".coverage"

        assert reused_coverage.keep(source, out, {"tests/test_a.py::kept"}, {}) == 1
        merged = Coverage(data_file=str(tmp_path / f"merged-{arcs}.coverage"), config_file=False)
        merged.combine(data_paths=[str(out)], keep=True)
        merged.xml_report(outfile=str(tmp_path / f"coverage-{arcs}.xml"))
        assert merged.get_data().measured_files() == {kept}
        assert merged.get_data().measured_contexts() == {"tests/test_a.py::kept|run"}
        data.read()
        assert data.measured_files() == {kept, missing}


def test_the_command_remaps_files_changed_since_the_dev_commit(tmp_path):
    repo = tmp_path / "repo"
    (repo / "hooks").mkdir(parents=True)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (repo / "hooks" / "a.py").write_text(OLD)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "dev"], check=True)
    commit = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    (repo / "hooks" / "a.py").write_text(NEW)
    subprocess.run([*git, "commit", "-qam", "head"], check=True)
    source = tmp_path / "dev.coverage"
    _record(source, {"tests/test_a.py::one|run": {"hooks/a.py": [(-1, 2), (2, -1)]}})
    reused = tmp_path / "reused.json"
    reused.write_text(json.dumps({"tests": ["tests/test_a.py::one"], "files": []}))
    out = tmp_path / "out" / ".coverage"
    args = ["--coverage", str(source), "--commit", commit, "--reused", str(reused), "--repo", str(repo)]
    args += ["--captures", str(tmp_path / "none"), "--captures-out", str(tmp_path / "js")]
    assert reused_coverage.main([*args, "--out", str(out)]) == 0
    assert _arcs(out) == {"hooks/a.py": {(-4, 5), (5, -4)}}


def test_the_dev_carry_leaves_out_tests_reused_from_pull_request_runs(tmp_path, monkeypatch):
    source = tmp_path / "dev.coverage"
    _record(
        source,
        {
            "tests/test_a.py::dev|run": {"hooks/a.py": [(-1, 2), (2, -1)]},
            "tests/test_a.py::branch@grp|run": {"hooks/a.py": [(-5, 6), (6, -5)]},
        },
    )
    reused = tmp_path / "reused.json"
    tests = ["tests/test_a.py::branch", "tests/test_a.py::dev"]
    reused.write_text(json.dumps({"tests": tests, "files": [], "branch": ["tests/test_a.py::branch"]}))
    monkeypatch.setattr(reused_coverage, "moved_lines", lambda repo, commit, head: {})
    out = tmp_path / "out" / ".coverage"
    args = ["--coverage", str(source), "--commit", "a" * 40, "--reused", str(reused)]
    args += ["--captures", str(tmp_path / "none"), "--captures-out", str(tmp_path / "js")]
    assert reused_coverage.main([*args, "--out", str(out)]) == 0
    assert _arcs(out) == {"hooks/a.py": {(-1, 2), (2, -1)}}


def _truncate(path: Path) -> None:
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])


def test_a_malformed_dev_coverage_fails_without_changing_the_shared_reuse_plan(tmp_path, monkeypatch, capsys):
    source = tmp_path / "dev.coverage"
    _record(source, {"tests/test_a.py::one|run": {"hooks/a.py": [(-1, 2), (2, -1)]}})
    _truncate(source)
    reused = tmp_path / "reused.json"
    plan = json.dumps({"tests": ["tests/test_a.py::one"], "files": []})
    reused.write_text(plan)
    monkeypatch.setenv("ARTIFACT", "coverage-3.12-9")
    args = ["--coverage", str(source), "--commit", "HEAD", "--reused", str(reused), "--repo", str(ROOT)]
    args += ["--captures", str(tmp_path / "none"), "--captures-out", str(tmp_path / "js")]
    status = reused_coverage.main([*args, "--out", str(tmp_path / "out" / ".coverage")])
    output = capsys.readouterr().out
    assert status == 1
    assert reused.read_text() == plan
    assert not (tmp_path / "out" / ".coverage").exists()
    errors = [line for line in output.splitlines() if line.startswith("::error::")]
    assert len(errors) == 1
    assert "coverage-3.12-9 from HEAD is unusable (" in errors[0]
    assert "database" in errors[0]


def test_the_check_names_a_truncated_or_tableless_database_and_passes_a_recorded_one(tmp_path):
    good = tmp_path / "good.coverage"
    _record(good, {"tests/test_a.py::one|run": {"hooks/a.py": [(-1, 2), (2, -1)]}})
    assert coverage_check.problem(good) is None
    bad = tmp_path / "bad.coverage"
    _record(bad, {"tests/test_a.py::one|run": {"hooks/a.py": [(-1, 2), (2, -1)]}})
    _truncate(bad)
    assert coverage_check.problem(bad)
    empty = tmp_path / "empty.coverage"
    empty.write_bytes(b"")
    assert "arc" in coverage_check.problem(empty)
    assert coverage_check.problem(tmp_path / "absent.coverage") == "missing"
    assert coverage_check.main([str(good)]) == 0
    assert coverage_check.main([str(good), str(bad)]) == 1


def test_the_unit_job_checks_its_coverage_database_before_uploading_it():
    steps = _steps("unit")
    names = list(steps)
    check = steps["Check the coverage database"]
    assert check["if"] == steps["Upload coverage"]["if"]
    assert "databases=(.coverage)" in check["run"]
    assert "databases+=(reused-coverage/.coverage)" in check["run"]
    assert 'python -m tests.coverage_check "${databases[@]}"' in check["run"]
    assert names.index("Run tests") < names.index("Check the coverage database") < names.index("Upload coverage")
    kept = steps["Keep the reused tests' dev coverage"]
    assert kept["env"]["ARTIFACT"] == "coverage-${{ matrix.python-version }}-${{ matrix.shard }}"


def test_a_partial_clone_fetches_every_changed_file_old_text_in_one_batch(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    git = ["-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "-C", str(origin), *git, "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(origin), "config", "uploadpack.allowFilter", "true"], check=True)
    subprocess.run(["git", "-C", str(origin), "config", "uploadpack.allowAnySHA1InWant", "true"], check=True)
    names = [f"hooks/m{index}.py" for index in range(3)]
    (origin / "hooks").mkdir()
    for index, name in enumerate(names):
        (origin / name).write_text(f"{OLD}# {index}\n")
    subprocess.run(["git", "-C", str(origin), *git, "add", "."], check=True)
    subprocess.run(["git", "-C", str(origin), *git, "commit", "-qm", "dev"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    for index, name in enumerate(names):
        (origin / name).write_text(f"{NEW}# {index}\n")
    subprocess.run(["git", "-C", str(origin), *git, "commit", "-qam", "head"], check=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--filter=blob:none", origin.as_uri(), str(clone)], check=True)
    trace = tmp_path / "trace.json"
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    moved = reused_coverage.moved_lines(clone, commit, "HEAD")
    assert moved == {name: reused_coverage.line_map(f"{OLD}# 0\n", f"{NEW}# 0\n") for name in names}
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    fetches = [e for e in events if e.get("event") == "child_start" and "fetch" in e.get("argv", [])]
    assert len(fetches) == 1


def test_pull_requests_into_dev_plan_reuse_from_the_protected_dev_grader():
    plan = _steps("test-count")["Plan the tests whose inputs did not change"]
    assert "github.event.pull_request.head.repo.full_name == github.repository" in plan["if"]
    assert "'origin/dev'" in plan["env"]["GRADER"]
    outputs = yaml.safe_load(WORKFLOW.read_text())["jobs"]["split"]["outputs"]
    assert outputs["run"] == "${{ steps.plan.outputs.run }}"
    assert outputs["commit"] == "${{ steps.plan.outputs.commit }}"


def test_coverage_shards_ship_the_reused_tests_dev_coverage_to_sonar_and_the_ratchet():
    steps = _steps("unit")
    names = list(steps)
    kept = steps["Keep the reused tests' dev coverage"]
    assert "python -m tests.reused_coverage" in kept["run"]
    assert 'cd "$RUNNER_TEMP/grader"' in kept["run"]
    assert kept["env"]["GRADER"] == "${{ github.event_name == 'pull_request' && 'origin/dev' || github.sha }}"
    assert '--out "$GITHUB_WORKSPACE/reused-coverage/.coverage"' in kept["run"]
    assert '--captures-out "$GITHUB_WORKSPACE/js-coverage"' in kept["run"]
    assert "github.event_name != 'merge_group'" in kept["if"]
    assert names.index("Download the tests this run reuses") < names.index("Keep the reused tests' dev coverage")
    assert names.index("Keep the reused tests' dev coverage") < names.index("Run tests")
    assert "reused-coverage/" in steps["Upload coverage"]["with"]["path"]
    combine = (ROOT / ".github" / "coverage" / "combine.sh").read_text()
    assert "coverage-3.12-$shard/reused-coverage/.coverage" in combine


def test_a_function_with_repeated_short_lines_keeps_its_own_lines_after_an_earlier_function_changes():
    body = "    if x:\n        return None\n    else:\n        return None\n"
    old = "def a(x):\n" + body + "\n\ndef b(x):\n" + body
    new = "def a(x):\n    y = x\n" + body + "\n\ndef b(x):\n" + body
    lines = reused_coverage.line_map(old, new)
    assert [lines[number] for number in range(8, 13)] == list(range(9, 14))


def test_only_page_captures_of_reused_tests_or_of_no_test_are_kept(tmp_path):
    source, out = tmp_path / "dev", tmp_path / "head"
    (source / "sources").mkdir(parents=True)
    for name in ("kept", "rerun", "outside", "legacy"):
        (source / "sources" / f"{name}.js").write_text(name)
    captures = {
        "capture-browser-1.json": {"result": [{"source": "kept"}], "test": "tests/test_a.py::kept@grp (teardown)"},
        "capture-node-2.json": {"result": [{"source": "rerun"}], "test": "tests/test_a.py::rerun (call)"},
        "capture-browser-3.json": {"result": [{"source": "outside"}], "test": ""},
        "capture-browser-4.json": {"result": [{"source": "legacy"}]},
    }
    for name, recorded in captures.items():
        (source / name).write_text(json.dumps(recorded))
    assert reused_coverage.keep_captures(source, out, {"tests/test_a.py::kept"}) == 3
    assert sorted(path.name for path in out.glob("capture-*.json")) == [
        "capture-browser-1.json",
        "capture-browser-3.json",
        "capture-browser-4.json",
    ]
    assert sorted(path.name for path in (out / "sources").iterdir()) == ["kept.js", "legacy.js", "outside.js"]
