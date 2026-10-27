import ast
import hashlib
import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest
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


def _deselect_in_test_count(tmp_path, head_manifest):
    collected = ["tests/test_a.py::one", "tests/test_a.py::two", "tests/test_b.py::one", "tests/test_b.py::new"]
    reused = ["tests/test_a.py::one", "tests/test_a.py::two", "tests/test_b.py::one"]
    runner = tmp_path / "runner"
    workspace = tmp_path / "workspace"
    for checkout, manifest in (
        (runner / "planner", (ROOT / "tests" / "dep_manifest.py").read_text()),
        (workspace, head_manifest),
    ):
        (checkout / "tests").mkdir(parents=True)
        (checkout / "tests" / "__init__.py").write_text("")
        (checkout / "tests" / "dep_manifest.py").write_text(manifest)
    (workspace / "reused-tests.json").write_text(json.dumps({"tests": reused}))
    (runner / "collected.json").write_text(json.dumps(collected))
    env = {
        **os.environ,
        "RUNNER_TEMP": str(runner),
        "GITHUB_WORKSPACE": str(workspace),
        "PYTHON_PATH": sys.executable,
        "DEV_PLAN": str(workspace / "reused-tests.json"),
    }
    run = _step("test-count", "Deselect the reused tests")["run"]
    subprocess.run(["bash", "-eo", "pipefail", "-c", run], cwd=workspace, env=env, check=True)
    return json.loads((runner / "reused.json").read_text()), dep_manifest.deselect(reused, collected)


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


def test_the_manifest_preserves_each_contexts_executed_lines(tmp_path):
    manifest = _measure(tmp_path)
    assert manifest["lines"]["tests/test_a.py::test_double"] == {"hooks/sample.py": [7]}
    assert manifest["lines"]["tests/test_b.py::test_grow[1]"] == {"hooks/sample.py": [14, 15, 17]}
    assert "tests/test_c.py::test_nothing" not in manifest["lines"]


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


def test_the_inputs_plugin_records_files_a_child_process_reads_for_its_test_only(tmp_path):
    (tmp_path / "child.txt").write_text("x")
    (tmp_path / "test_sample.py").write_text(
        "import subprocess, sys\n"
        "def test_child_reads():\n"
        "    subprocess.run([sys.executable, '-c', \"open('child.txt').read()\"], env={}, check=True)\n"
        "def test_child_reads_nothing():\n"
        "    subprocess.run([sys.executable, '-c', 'pass'], env={}, check=True)\n"
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
        {"test": "test_sample.py::test_child_reads", "files": ["child.txt"], "escapes": True},
        {"test": "test_sample.py::test_child_reads_nothing", "files": [], "escapes": True},
    ]
    name = hashlib.sha256(str(out / "children").encode()).hexdigest()[:16]
    assert not (Path(sysconfig.get_path("purelib")) / f"agentihooks-dep-inputs-{name}.pth").exists()


def test_the_coverage_shards_record_per_test_contexts_and_inputs():
    run = _step("unit", "Run tests")
    coverage_options = run["env"]["PYTEST_ADDOPTS"].split("||")[0]
    assert "--cov-context=test" in coverage_options
    assert "-p tests.dep_inputs" in coverage_options
    assert "dep-inputs" in run["env"]["DEP_INPUTS_DIR"].split("||")[0]
    assert "dep-inputs/" in _step("unit", "Upload coverage")["with"]["path"]


def test_sonar_builds_the_manifest_and_only_dev_pushes_save_it():
    steps = [step.get("name") for step in _job("sonar")["steps"]]
    assert (
        steps.index("Merge shard coverage")
        < steps.index("Build the test dependency manifest")
        < steps.index("Start Cloudflare Access proxy")
    )
    build = _step("sonar", "Build the test dependency manifest")
    assert "python -m tests.dep_manifest build" in build["run"]
    assert "< /dev/null" in build["run"] and 'echo "pid=$!"' in build["run"]
    assert '--out "$RUNNER_TEMP/dep-manifest/manifest.json"' in build["run"]
    contexts = '--data-file="$RUNNER_TEMP/dep-manifest/.coverage"'
    assert f"python -m coverage combine --rcfile=.github/coverage/coverage.ini --keep {contexts}" in build["run"]
    assert '--coverage "$RUNNER_TEMP/dep-manifest/.coverage"' in build["run"]
    assert build["run"].index(contexts) < build["run"].index("python -m tests.dep_manifest build")
    assert (
        steps.index("Build the test dependency manifest")
        < steps.index("Wait for the test dependency manifest")
        < steps.index("SonarQube Scan")
        < steps.index("Upload the test dependency manifest")
    )
    wait = _step("sonar", "Wait for the test dependency manifest")
    assert wait["env"]["MANIFEST_PID"] == "${{ steps.manifest.outputs.pid }}"
    assert 'exit "$(< "$RUNNER_TEMP/manifest.status")"' in wait["run"]
    upload = _step("sonar", "Upload the test dependency manifest")
    assert upload["with"]["name"] == "dep-manifest"
    assert upload["with"]["path"] == "${{ runner.temp }}/dep-manifest/manifest.json"
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


@pytest.mark.parametrize("path", [".github/workflows/swarm-smoke.yml", ".github/workflows/test.yml"])
def test_a_workflow_only_plant_reuses_unrelated_tests_and_a_graded_plant_runs_its_tests(path):
    manifest = {
        "version": 2,
        "commit": "c" * 40,
        "units": ["hooks/sample.py::double", "hooks/sample.py::<module>"],
        "tests": {
            "tests/test_workflow.py::test_workflow": {"units": [], "files": [path], "escapes": False},
            "tests/test_math.py::test_double": {"units": [0, 1], "files": [], "escapes": False},
            "tests/test_other.py::test_other": {"units": [], "files": [], "escapes": False},
        },
    }
    old = "jobs:\n  unit:\n    steps: []\n  split:\n    steps: []\n  lint:\n    name: before\n"
    new = old.replace("name: before", "name: after")
    chosen = dep_manifest.plan(manifest, [(path, old, new)], "41")
    assert chosen == {
        "run": "41",
        "commit": "c" * 40,
        "global": None,
        "invalidated": 1,
        "tests": ["tests/test_math.py::test_double", "tests/test_other.py::test_other"],
    }
    graded = [("hooks/sample.py", SOURCE, SOURCE.replace("value * 2", "value * 3"))]
    assert dep_manifest.plan(manifest, graded, "41")["tests"] == [
        "tests/test_other.py::test_other",
        "tests/test_workflow.py::test_workflow",
    ]


@pytest.mark.parametrize("shared_import", [None, "helper", "test_reader", "dynamic"])
def test_collection_workflow_inputs_belong_only_to_the_module_that_reads_them(tmp_path, shared_import):
    workflow = tmp_path / ".github" / "workflows" / "smoke.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: smoke\n")
    (tmp_path / "test_reader.py").write_text(
        "from pathlib import Path\n"
        "WORKFLOW = Path('.github/workflows/smoke.yml').read_text()\n"
        "def test_reads():\n    assert WORKFLOW == 'name: smoke\\n'\n"
    )
    consumers = []
    if shared_import:
        if shared_import == "helper":
            (tmp_path / "helper.py").write_text(
                "from pathlib import Path\nWORKFLOW = Path('.github/workflows/smoke.yml').read_text()\n"
            )
            (tmp_path / "test_reader.py").write_text(
                "from helper import WORKFLOW\ndef test_reads():\n    assert WORKFLOW == 'name: smoke\\n'\n"
            )
        imported = (
            "import importlib\nWORKFLOW = importlib.import_module('test_reader').WORKFLOW\n"
            if shared_import == "dynamic"
            else f"from {shared_import} import WORKFLOW\n"
        )
        (tmp_path / "test_consumer.py").write_text(
            imported + "def test_cached():\n    assert WORKFLOW == 'name: smoke\\n'\n"
        )
        consumers = ["test_consumer.py"]
    (tmp_path / "test_unrelated.py").write_text("def test_other():\n    assert 2 + 2 == 4\n")
    out = tmp_path / "inputs"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "tests.dep_inputs",
            "-p",
            "no:cacheprovider",
            "test_reader.py",
            *consumers,
            "test_unrelated.py",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT), "DEP_INPUTS_DIR": str(out), "PYTEST_ADDOPTS": ""},
        check=True,
        capture_output=True,
    )
    rows = [json.loads(line) for report in out.glob("*.jsonl") for line in report.read_text().splitlines()]
    expected = (
        []
        if not shared_import
        else [
            {
                "test": "test_consumer.py::test_cached",
                "files": [] if shared_import == "dynamic" else [".github/workflows/smoke.yml"],
                "escapes": shared_import == "dynamic",
            }
        ]
    )
    assert sorted(rows, key=lambda row: row["test"]) == expected + [
        {"test": "test_reader.py::test_reads", "files": [".github/workflows/smoke.yml"], "escapes": False},
        {"test": "test_unrelated.py::test_other", "files": [], "escapes": False},
    ]


@pytest.mark.parametrize(
    "new",
    [
        None,
        "jobs: [broken]",
        "jobs:\n  unit: [broken]\n  split: {}\n",
        "jobs:\n  unit: {}\n  split: {}\nenv:\n  MODE: changed\n",
        "jobs:\n  unit: {}\n  split: {}\ndefaults:\n  run:\n    shell: python\n",
        "jobs:\n  unit:\n    steps: [{run: changed}]\n  split: {}\n",
        "jobs:\n  unit:\n    needs: select\n  split: {}\n  select:\n    steps: [{run: changed}]\n",
    ],
)
def test_execution_input_plants_still_invalidate_every_test(tmp_path, new):
    manifest = _measure(tmp_path)
    old = "jobs:\n  unit:\n    needs: select\n  split: {}\n  select: {}\n"
    result = dep_manifest.plan(manifest, [(".github/workflows/test.yml", old, new)], "41")
    assert result["tests"] == []
    assert result["global"] == ".github/workflows/test.yml"


@pytest.mark.parametrize("version", [None, 1])
def test_legacy_manifests_keep_workflow_changes_global(tmp_path, version):
    manifest = _measure(tmp_path)
    manifest["version"] = version
    result = dep_manifest.plan(manifest, [(".github/workflows/smoke.yml", "name: old", "name: new")], "41")
    assert result["tests"] == []
    assert result["global"] == ".github/workflows/smoke.yml"


@pytest.mark.parametrize("path", [".github/test-excludes.txt", ".github/actions/retry-download/action.yml"])
def test_shared_github_execution_inputs_remain_global(tmp_path, path):
    manifest = _measure(tmp_path)
    result = dep_manifest.plan(manifest, [(path, "old", "new")], "41")
    assert result["tests"] == []
    assert result["global"] == path


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
    plan = _step("test-count", "Plan the tests whose inputs did not change")
    assert plan["env"]["GRADER"] == (
        "${{ github.event.merge_group.base_sha || github.event_name == 'pull_request' && 'origin/dev' || github.sha }}"
    )
    assert 'cd "$RUNNER_TEMP/planner"' in plan["run"]
    assert _job("split")["outputs"]["reused"] == "${{ steps.plan.outputs.reused }}"
    assert _job("unit")["outputs"]["reused"] == "${{ needs.split.outputs.reused }}"
    assert _step("test-count", "Wait for the test plan")["id"] == "plan"
    assert _step("split", "Adopt the test plan")["id"] == "plan"
    deselected = _step("test-count", "Deselect the reused tests")
    assert deselected["run"].startswith('cd "$RUNNER_TEMP/planner"\n"$PYTHON_PATH" -m tests.dep_manifest deselect')
    assert deselected["env"]["PYTHON_PATH"] == plan["env"]["PYTHON_PATH"]
    assert _step("unit", "Run tests")["env"]["CI_REUSED"] == "${{ github.workspace }}/reused.json"
    check = _step("shard-check", "Check every collected test ran in exactly one shard")
    assert check["run"].endswith(" $REUSED")
    assert "reused-plan/reused-tests.json" in check["env"]["REUSED"]


def _imports(module):
    path = ROOT / Path(*module.split("."))
    path = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
    loaded, lazy = [], []
    for statement in ast.parse(path.read_text()).body:
        names = lazy if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) else loaded
        for node in ast.walk(statement):
            if isinstance(node, ast.Import):
                names += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names += [node.module] + [f"{node.module}.{a.name}" for a in node.names]
    return loaded, lazy


def test_the_manifest_code_needs_only_yaml_beyond_the_standard_library():
    pending, seen = ["scripts", "tests", "scripts.ci_test_plan", "tests.dep_manifest"], set()
    third_party = {"loaded": set(), "lazy": set()}
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        for kind, names in zip(("loaded", "lazy"), _imports(module), strict=True):
            for name in names:
                parts = name.split(".")
                if parts[0] not in {"scripts", "tests", "hooks"}:
                    # Mutation runs read mutmut's trampoline import in the mutated copy.
                    harness = parts[0] == "mutmut" and "MUTANT_UNDER_TEST" in os.environ
                    if parts[0] not in sys.stdlib_module_names and not harness:
                        third_party[kind].add(parts[0])
                    continue
                for depth in range(1, len(parts) + 1):
                    candidate = ROOT / Path(*parts[:depth])
                    if candidate.is_dir() or candidate.with_suffix(".py").is_file():
                        pending.append(".".join(parts[:depth]))
    assert third_party == {"loaded": {"yaml"}, "lazy": {"coverage"}}


def test_every_bare_interpreter_call_into_the_manifest_code_installs_pyyaml_first():
    calls = set()
    for name, job in yaml.safe_load(WORKFLOW.read_text())["jobs"].items():
        steps = job.get("steps", [])
        for index, step in enumerate(steps):
            run = step.get("run", "")
            for call in ("-m scripts.ci_test_plan", "-m tests.dep_manifest"):
                if call in run:
                    calls.add((name, step["name"]))
                    earlier = [s.get("run", "") for s in steps[:index]]
                    lines = "\n".join([*earlier, run[: run.index(call)]]).splitlines()
                    assert any(
                        ("-m pip install" in line or '"$HOME/venv/bin/uv" pip install' in line)
                        and "PyYAML==6.0.3" in line
                        for line in lines
                    ), (
                        name,
                        step["name"],
                    )
    assert calls == {
        ("test-count", "Plan the tests whose inputs did not change"),
        ("test-count", "Deselect the reused tests"),
        ("sonar", "Build the test dependency manifest"),
    }


def test_the_split_deselects_with_the_grader_manifest_code_not_the_heads(tmp_path):
    planted = (
        (ROOT / "tests" / "dep_manifest.py")
        .read_text()
        .replace(
            "def deselect(",
            "def deselect(reused, collected):\n"
            "    return {'tests': sorted(collected), 'files': sorted({n.split('::')[0] for n in collected})}\n\n\n"
            "def _planted(",
            1,
        )
    )
    chosen, expected = _deselect_in_test_count(tmp_path, planted)
    assert chosen == expected


def test_the_split_deselects_the_same_tests_when_the_head_keeps_the_manifest_code(tmp_path):
    chosen, expected = _deselect_in_test_count(tmp_path, (ROOT / "tests" / "dep_manifest.py").read_text())
    assert (
        chosen
        == expected
        == {
            "tests": ["tests/test_a.py::one", "tests/test_a.py::two", "tests/test_b.py::one"],
            "files": ["tests/test_a.py"],
        }
    )


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


def test_a_packed_manifest_stores_each_file_list_and_line_span_once_and_unpacks_to_the_same_manifest():
    files = ["hooks/a.py", "tests/conftest.py"]
    manifest = {
        "version": 2,
        "commit": "c",
        "environment": "e",
        "units": ["hooks/a.py::a", "hooks/b.py::b"],
        "tests": {
            "tests/test_a.py::test_one": {"units": [0], "files": list(files), "escapes": False},
            "tests/test_a.py::test_two": {"units": [0, 1], "files": list(files), "escapes": True},
            "tests/test_b.py::test_b": {"units": [1], "files": [], "escapes": False},
        },
        "lines": {
            "tests/test_a.py::test_one": {"hooks/a.py": [1, 2]},
            "tests/test_a.py::test_two": {"hooks/a.py": [1, 2], "hooks/b.py": [5]},
        },
    }
    packed = dep_manifest.pack(json.loads(json.dumps(manifest)))
    assert packed["files"] == [files, []]
    assert packed["spans"] == [["hooks/a.py", [1, 2]], ["hooks/b.py", [5]]]
    assert packed["tests"]["tests/test_a.py::test_two"] == [[0, 1], 0, True]
    assert packed["lines"]["tests/test_a.py::test_two"] == [0, 1]
    assert {key: packed[key] for key in ("version", "commit", "environment", "units")} == {
        key: manifest[key] for key in ("version", "commit", "environment", "units")
    }
    assert dep_manifest.unpack(json.loads(json.dumps(packed))) == manifest


def test_a_packed_manifest_without_lines_unpacks_without_lines():
    manifest = {"commit": "c", "units": [], "tests": {"t": {"units": [], "files": ["x"], "escapes": False}}}
    assert dep_manifest.unpack(dep_manifest.pack(manifest)) == manifest


def test_the_build_writes_the_packed_manifest_only_when_asked_and_the_workflow_uploads_it(tmp_path, monkeypatch):
    built = {"version": 2, "commit": "c", "units": [], "tests": {"t": {"units": [], "files": ["x"], "escapes": False}}}
    monkeypatch.setattr(dep_manifest, "build", lambda *args: built)
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "a.jsonl").write_text("")
    out, packed = tmp_path / "manifest.json", tmp_path / "packed.json"
    argv = ["build", "--coverage", "c", "--inputs", str(tmp_path / "inputs"), "--commit", "c", "--environment", "e"]
    assert dep_manifest.main([*argv, "--out", str(out)]) == 0
    assert not packed.exists()
    assert dep_manifest.main([*argv, "--out", str(out), "--packed", str(packed)]) == 0
    assert json.loads(out.read_text()) == built
    assert dep_manifest.unpack(json.loads(packed.read_text())) == built
    build = _step("sonar", "Build the test dependency manifest")
    assert '--packed "$RUNNER_TEMP/dep-manifest/packed.json"' in build["run"]
    upload = _step("sonar", "Upload the packed test dependency manifest")
    assert upload["if"] == _step("sonar", "Upload the test dependency manifest")["if"]
    assert upload["with"]["name"] == "dep-manifest-packed"
    assert upload["with"]["path"] == "${{ runner.temp }}/dep-manifest/packed.json"
    assert upload["with"]["if-no-files-found"] == "error"
