import json
import subprocess
from pathlib import Path

import yaml
from coverage import CoverageData

from tests import reused_coverage

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


def test_pull_requests_into_dev_plan_reuse_from_the_protected_dev_grader():
    plan = _steps("split")["Plan the tests whose inputs did not change"]
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
