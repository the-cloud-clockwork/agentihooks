import json
import os
import subprocess
import sys
from pathlib import Path

import yaml
from coverage import CoverageData

from tests import dep_manifest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"

SOURCE = """import os

LIMIT = 3


def double(value):
    return value * 2


class Box:
    size = LIMIT

    def grow(self, value):
        def inner():
            return value + 1

        return inner()


def unused():
    return os.sep
"""


def _job(name):
    return yaml.safe_load(WORKFLOW.read_text())["jobs"][name]


def _step(job, name):
    return next(step for step in _job(job)["steps"] if step.get("name") == name)


def test_lines_map_to_the_innermost_function_and_definitions_to_their_scope():
    units = dep_manifest.line_units(SOURCE)
    assert units[7] == "double"
    assert units[15] == "Box.grow.inner"
    assert units[14] == units[17] == "Box.grow"
    assert 6 not in units and 11 not in units and 13 not in units


def test_a_body_change_moves_only_its_function_and_a_new_function_moves_nothing():
    assert dep_manifest.changed_units(SOURCE, SOURCE.replace("value * 2", "value * 3")) == {"double"}
    assert dep_manifest.changed_units(SOURCE, SOURCE + "\n\ndef added():\n    return 1\n") == {"added"}
    assert dep_manifest.changed_units(SOURCE, SOURCE.replace("LIMIT = 3", "LIMIT = 4")) == {"<module>"}
    assert dep_manifest.changed_units(SOURCE, SOURCE.replace("value + 1", "value + 2")) == {
        "Box.grow",
        "Box.grow.inner",
    }
    assert dep_manifest.changed_units(SOURCE, "def broken(:\n") >= {"<module>", "double", "Box.grow", "unused"}
    assert dep_manifest.changed_units(SOURCE, None) == set(dep_manifest.unit_hashes(SOURCE))
    assert dep_manifest.changed_units(SOURCE, SOURCE.replace("LIMIT = 3", "# limit\nLIMIT = 3")) == set()
    feed = "A = 1\n\fB = 2\n\n\ndef f():\n    return 1\n"
    assert dep_manifest.changed_units(feed, feed.replace("return 1", "return 2")) == {"f"}
    texts = 'NOTE = """\n# one\n"""\n'
    assert dep_manifest.changed_units(texts, texts.replace("# one", "# two")) == {"<module>"}


def test_a_build_without_input_reports_is_red(tmp_path, capsys):
    (tmp_path / "inputs").mkdir()
    argv = ["build", "--coverage", str(tmp_path / ".coverage"), "--inputs", str(tmp_path / "inputs")]
    assert dep_manifest.main([*argv, "--commit", "abc", "--environment", "x", "--out", str(tmp_path / "m.json")]) == 1
    assert "No dependency input reports" in capsys.readouterr().err


def _measure(tmp_path):
    package = tmp_path / "hooks"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "sample.py").write_text(SOURCE)
    path = str(package / "sample.py")
    data = CoverageData(basename=str(tmp_path / ".coverage"))
    measured = {
        "": [(-1, 1), (1, 3), (3, 6), (6, 10), (10, 11), (11, 13), (13, 20), (20, -1)],
        "tests/test_a.py::test_double|run": [(-6, 7), (7, -6)],
        "tests/test_b.py::test_grow[1]|run": [(-13, 14), (14, 17), (17, -13), (-14, 15), (15, -14)],
    }
    for context, arcs in measured.items():
        data.set_context(context)
        data.add_arcs({path: arcs})
    data.write()
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "w.jsonl").write_text(
        json.dumps({"test": "tests/test_c.py::test_nothing", "files": ["docs/data.json"], "escapes": True}) + "\n"
    )
    return dep_manifest.build(tmp_path / ".coverage", sorted(inputs.glob("*.jsonl")), tmp_path, "abc", "py3.12-x")


def _units(manifest, test):
    return {manifest["units"][number] for number in manifest["tests"][test]["units"]}


def test_the_manifest_holds_the_functions_each_test_executed(tmp_path):
    manifest = _measure(tmp_path)
    assert manifest["commit"] == "abc" and manifest["environment"] == "py3.12-x"
    assert _units(manifest, "tests/test_a.py::test_double") == {"hooks/sample.py::double", "hooks/sample.py::<module>"}
    assert _units(manifest, "tests/test_b.py::test_grow[1]") == {
        "hooks/sample.py::Box.grow",
        "hooks/sample.py::Box.grow.inner",
        "hooks/sample.py::<module>",
    }
    assert manifest["tests"]["tests/test_c.py::test_nothing"] == {
        "units": [],
        "files": ["docs/data.json"],
        "escapes": True,
    }


def test_a_planted_change_to_one_function_invalidates_exactly_the_tests_that_ran_it(tmp_path):
    manifest = _measure(tmp_path)
    planted = [("hooks/sample.py", SOURCE, SOURCE.replace("value * 2", "value * 3"))]
    hit, cause = dep_manifest.invalidated(manifest, planted)
    assert cause is None
    assert hit == {"tests/test_a.py::test_double", "tests/test_c.py::test_nothing"}
    unrun = [("hooks/sample.py", SOURCE, SOURCE.replace("os.sep", "os.pathsep"))]
    assert dep_manifest.invalidated(manifest, unrun)[0] == {"tests/test_c.py::test_nothing"}


def test_tests_files_and_global_inputs_invalidate_their_dependants(tmp_path):
    manifest = _measure(tmp_path)
    assert dep_manifest.invalidated(manifest, [("tests/test_b.py", "", "x")])[0] == {
        "tests/test_b.py::test_grow[1]",
        "tests/test_c.py::test_nothing",
    }
    assert dep_manifest.invalidated(manifest, [("tests/conftest.py", "", "x")])[0] == set(manifest["tests"])
    assert dep_manifest.invalidated(manifest, [("tests/helpers/x.py", "", "x")])[0] == set(manifest["tests"])
    escaped = dep_manifest.invalidated(manifest, [("tests/data/x.json", None, None)])[0]
    assert escaped == {"tests/test_c.py::test_nothing"}
    assert dep_manifest.invalidated(manifest, [("docs/data.json", None, None)])[0] == {"tests/test_c.py::test_nothing"}
    assert dep_manifest.invalidated(manifest, [("docs/other.md", None, None)])[0] == set()
    hit, cause = dep_manifest.invalidated(manifest, [(".github/workflows/test.yml", None, None)])
    assert hit == set(manifest["tests"]) and cause == ".github/workflows/test.yml"
    assert dep_manifest.invalidated(manifest, [("profiles/x.py", "", "y")])[1] == "profiles/x.py"


def test_changes_read_both_sides_of_a_git_diff(tmp_path):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "sample.py").write_text(SOURCE)
    (tmp_path / "notes.md").write_text("a")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    (tmp_path / "hooks" / "sample.py").write_text(SOURCE.replace("value * 2", "value * 3"))
    (tmp_path / "notes.md").write_text("b")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "head")
    found = dep_manifest.changes(tmp_path, "HEAD^1", "HEAD")
    assert found == [
        ("hooks/sample.py", SOURCE, SOURCE.replace("value * 2", "value * 3")),
        ("notes.md", None, None),
    ]


def test_the_inputs_plugin_records_opened_files_and_escapes(tmp_path):
    (tmp_path / "data.txt").write_text("x")
    (tmp_path / "shared.txt").write_text("x")
    (tmp_path / "test_sample.py").write_text(
        "import subprocess, sys\nfrom pathlib import Path\n"
        "SHARED = open('shared.txt').read()\n"
        "def test_reads():\n    open(Path('data.txt')).read()\n"
        "def test_spawns():\n    subprocess.run([sys.executable, '-c', 'pass'], check=True)\n"
        "import pytest\n"
        "@pytest.fixture(scope='session')\n"
        "def server():\n    subprocess.run([sys.executable, '-c', 'pass'], check=True)\n"
        "def test_first(server):\n    pass\n"
        "def test_second(server):\n    pass\n"
    )
    out = tmp_path / "inputs"
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "tests.dep_inputs", "-p", "no:cacheprovider", "test_sample.py"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT), "DEP_INPUTS_DIR": str(out), "PYTEST_ADDOPTS": ""},
        check=True,
        capture_output=True,
    )
    lines = [json.loads(line) for report in out.glob("*.jsonl") for line in report.read_text().splitlines()]
    assert sorted(lines, key=lambda line: line["test"]) == [
        {"test": "test_sample.py::test_first", "files": ["shared.txt"], "escapes": True},
        {"test": "test_sample.py::test_reads", "files": ["data.txt", "shared.txt"], "escapes": False},
        {"test": "test_sample.py::test_second", "files": ["shared.txt"], "escapes": True},
        {"test": "test_sample.py::test_spawns", "files": ["shared.txt"], "escapes": True},
    ]


def test_the_coverage_shards_record_per_test_contexts_and_inputs():
    run = _step("unit", "Run tests")
    coverage_options = run["env"]["PYTEST_ADDOPTS"].split("||")[0]
    assert "--cov-context=test" in coverage_options
    assert "-p tests.dep_inputs" in coverage_options
    assert "dep-inputs" in run["env"]["DEP_INPUTS_DIR"].split("||")[0]
    assert "dep-inputs/" in _step("unit", "Upload coverage")["with"]["path"]


def test_sonar_builds_the_manifest_and_only_dev_pushes_save_it():
    steps = [step.get("name") for step in _job("sonar")["steps"]]
    assert steps.index("Wait for the coverage merge") < steps.index("Build the test dependency manifest")
    build = _step("sonar", "Build the test dependency manifest")
    assert "python -m tests.dep_manifest build" in build["run"]
    assert _step("sonar", "Upload the test dependency manifest")["with"]["name"] == "dep-manifest"
    assert not any("cache/save" in step.get("uses", "") for step in _job("sonar")["steps"])
    assert "github.event_name == 'push'" in _job("coverage-baseline")["if"]
    assert _step("coverage-baseline", "Download the test dependency manifest")["with"]["name"] == "dep-manifest"
    save = _step("coverage-baseline", "Save the dev test dependency manifest")
    assert save["if"] == "needs.gate-required.result == 'success'"
    assert save["uses"].startswith("actions/cache/save@")
    assert save["with"]["key"] == "dep-manifest-${{ github.sha }}"


def test_the_plan_reuses_every_test_the_change_left_valid_without_its_group_suffix():
    manifest = {
        "commit": "c" * 40,
        "units": ["hooks/sample.py::double", "hooks/sample.py::<module>"],
        "tests": {
            "tests/test_a.py::test_double@serial": {"units": [0, 1], "files": [], "escapes": False},
            "tests/test_b.py::test_module": {"units": [1], "files": [], "escapes": False},
            "tests/test_c.py::test_spawn[x@y]": {"units": [], "files": [], "escapes": True},
        },
    }
    planted = [("hooks/sample.py", SOURCE, SOURCE.replace("value * 2", "value * 3"))]
    assert dep_manifest.plan(manifest, planted, "41") == {
        "run": "41",
        "commit": "c" * 40,
        "global": None,
        "invalidated": 2,
        "tests": ["tests/test_b.py::test_module"],
    }
    assert dep_manifest.plan(manifest, [], "41")["tests"] == [
        "tests/test_a.py::test_double",
        "tests/test_b.py::test_module",
        "tests/test_c.py::test_spawn[x@y]",
    ]
    assert dep_manifest.plan(manifest, [(".github/workflows/test.yml", None, None)], "41")["tests"] == []


def test_deselection_keeps_new_tests_and_skips_only_wholly_reused_files(tmp_path):
    collected = ["tests/test_a.py::one", "tests/test_a.py::two", "tests/test_b.py::one", "tests/test_b.py::new"]
    reused = ["tests/test_a.py::one", "tests/test_a.py::two", "tests/test_b.py::one", "tests/test_gone.py::x"]
    assert dep_manifest.deselect(reused, collected) == {
        "tests": ["tests/test_a.py::one", "tests/test_a.py::two", "tests/test_b.py::one"],
        "files": ["tests/test_a.py"],
    }
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"tests": reused}))
    (tmp_path / "collected.json").write_text(json.dumps(collected))
    out = tmp_path / "reused.json"
    args = ["deselect", "--plan", str(plan), "--collected", str(tmp_path / "collected.json"), "--out", str(out)]
    assert dep_manifest.main(args) == 0
    assert json.loads(out.read_text())["files"] == ["tests/test_a.py"]


def test_queue_shards_split_on_the_tests_the_planner_did_not_reuse():
    plan = _step("split", "Plan the tests whose inputs did not change")
    assert plan["env"]["GRADER"] == "${{ github.event.merge_group.base_sha || github.sha }}"
    assert 'cd "$RUNNER_TEMP/planner"' in plan["run"]
    assert _job("split")["outputs"]["reused"] == "${{ steps.plan.outputs.reused }}"
    assert _job("unit")["outputs"]["reused"] == "${{ needs.split.outputs.reused }}"
    assert _step("split", "Deselect the reused tests")["run"].startswith("python -m tests.dep_manifest deselect")
    assert _step("unit", "Run tests")["env"]["CI_REUSED"] == "${{ github.workspace }}/reused.json"
    check = _step("shard-check", "Check every collected test ran in exactly one shard")
    assert check["run"].endswith(" $REUSED")
    assert "reused-plan/reused-tests.json" in check["env"]["REUSED"]


def test_the_reused_plan_reaches_workers_but_never_nested_pytest_runs(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import pytest

    from tests import conftest

    plan = tmp_path / "reused.json"
    plan.write_text(json.dumps({"tests": ["tests/test_a.py::one"], "files": ["tests/test_b.py"]}))
    monkeypatch.setenv("CI_REUSED", str(plan))
    controller = SimpleNamespace(stash=pytest.Stash())
    assert conftest._reused(controller) == (frozenset({"tests/test_a.py::one"}), frozenset({"tests/test_b.py"}))
    assert "CI_REUSED" not in os.environ
    node = SimpleNamespace(config=controller, workerinput={})
    conftest.pytest_configure_node(node)
    assert node.workerinput == {"ci_reused": str(plan)}
    worker = SimpleNamespace(stash=pytest.Stash(), workerinput=node.workerinput)
    assert conftest._reused(worker) == conftest._reused(controller)
    assert conftest._reused(SimpleNamespace(stash=pytest.Stash())) == (frozenset(), frozenset())
