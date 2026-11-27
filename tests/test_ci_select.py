import json
import os
import re
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import ci_select, locked_tests
from tests.shard_budget import step_seconds

pytestmark = pytest.mark.unit

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/test.yml"
EXPRESSION = re.compile(r"\$\{\{\s*([\w.]+)\s*\}\}")


def write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-b", "dev")
    git(tmp_path, "config", "user.email", "ci@example.invalid")
    git(tmp_path, "config", "user.name", "CI")
    write(
        tmp_path,
        {
            "hooks/sample.py": "VALUE = 1\n",
            "scripts/lonely.py": "VALUE = 2\n",
            "tests/test_sample.py": "from hooks import sample\n",
            "tests/test_page.py": "PAGE = 'ledger.js'\n",
            "tests/test_other.py": "import os\n",
            ".test_durations": "{}",
        },
    )
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    return tmp_path


def decide(root, changed, durations=None, limit=3):
    return ci_select.decide(root, changed, durations or {}, limit)


@pytest.mark.parametrize(
    "changed",
    [
        ["pyproject.toml"],
        [".github/test-excludes.txt"],
        ["tests/__init__.py"],
        ["tests/shards.py"],
        ["scripts/shard_sizing.py"],
        ["tests/flake_ledger.py"],
        ["tests/browser_coverage.py"],
        ["tests/node_capture.cjs"],
        ["tests/conftest.py"],
        ["conftest.py"],
        ["scripts/pkg/conftest.py"],
        ["tests/swarm/conftest.py", "tests/conftest.py"],
    ],
)
def test_a_change_to_the_test_harness_runs_the_full_suite(repo, changed):
    assert decide(repo, changed) == ("full", [])


def test_a_nested_conftest_selects_the_tests_beneath_it(repo: Path) -> None:
    write(
        repo,
        {
            "tests/pkg/conftest.py": "import os\n",
            "tests/pkg/test_near.py": "import os\n",
            "tests/pkg/deep/test_far.py": "import os\n",
            "tests/pkg/helper.py": "import os\n",
            "tests/other/test_beside.py": "import os\n",
        },
    )
    scope, legs = decide(repo, ["tests/pkg/conftest.py"])
    assert scope == "selected"
    assert sorted(path for leg in legs for path in leg) == ["tests/pkg/deep/test_far.py", "tests/pkg/test_near.py"]


def test_a_nested_module_beside_a_conftest_selects_only_the_tests_that_import_it(repo: Path) -> None:
    write(
        repo,
        {
            "tests/pkg/conftest.py": "import os\n",
            "tests/pkg/helper.py": "import os\n",
            "tests/pkg/test_near.py": "from tests.pkg import helper\n",
            "tests/pkg/test_far.py": "import os\n",
        },
    )
    assert decide(repo, ["tests/pkg/helper.py"]) == ("selected", [["tests/pkg/test_near.py"]])


PYPROJECT = """\
[build-system]
requires = ["setuptools>=80"]

[project]
name = "sample"
version = "1.0"
dependencies = ["pyyaml>=6"]

[project.optional-dependencies]
dev = ["pytest>=8"]

[tool.pytest.ini_options]
markers = ["unit"]

[tool.ruff]
line-length = 120
"""
PYPROJECT_SELECTED = "scope=selected\nlegs=[1]\ncount=1\nfiles_1=tests/test_build.py\n"
PYPROJECT_FULL = "scope=full\nlegs=[]\ncount=0\n"


def commit_pyproject(repo: Path, text: str, message: str) -> None:
    write(repo, {"pyproject.toml": text, "tests/test_build.py": "BUILD = 'pyproject.toml'\n"})
    git(repo, "add", "pyproject.toml", "tests/test_build.py")
    git(repo, "commit", "-m", message)


@pytest.mark.parametrize(
    ("old", "new", "written"),
    [
        ('version = "1.0"', 'version = "1.1"', PYPROJECT_SELECTED),
        ("line-length = 120", "line-length = 100", PYPROJECT_SELECTED),
        ('"pyyaml>=6"', '"pyyaml>=7"', PYPROJECT_FULL),
        ('"pytest>=8"', '"pytest>=9"', PYPROJECT_FULL),
        ('"setuptools>=80"', '"setuptools>=81"', PYPROJECT_FULL),
        ('markers = ["unit"]', 'markers = ["unit", "slow"]', PYPROJECT_FULL),
    ],
    ids=["version", "ruff", "dependencies", "optional-dependencies", "build-system", "pytest"],
)
def test_a_pyproject_change_runs_the_full_suite_only_when_it_reaches_tests(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, old: str, new: str, written: str
) -> None:
    commit_pyproject(repo, PYPROJECT, "pyproject")
    commit_pyproject(repo, PYPROJECT.replace(old, new), "change")
    assert run_main(repo, monkeypatch, tmp_path, "pull_request") == written


@pytest.mark.parametrize(
    ("old", "new", "asked"),
    [('version = "1.0"', 'version = "1.1"', ["owner/repo"]), ('"pyyaml>=6"', '"pyyaml>=7"', [])],
    ids=["version", "dependencies"],
)
def test_recorded_coverage_is_read_only_when_a_pyproject_change_skips_the_full_suite(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, old: str, new: str, asked: list[str]
) -> None:
    commit_pyproject(repo, PYPROJECT, "pyproject")
    commit_pyproject(repo, PYPROJECT.replace(old, new), "change")
    monkeypatch.setenv("CI_SELECT_REPOSITORY", "owner/repo")
    calls: list[str] = []

    def record(repository: str, before: str) -> None:
        calls.append(repository)

    monkeypatch.setattr(ci_select, "find_record", record)
    run_main(repo, monkeypatch, tmp_path, "pull_request")
    assert calls == asked


def test_a_new_pyproject_runs_the_full_suite(repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    commit_pyproject(repo, PYPROJECT, "pyproject")
    assert run_main(repo, monkeypatch, tmp_path, "pull_request") == PYPROJECT_FULL


def test_a_pyproject_compares_the_pull_request_against_its_merge_base(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commit_pyproject(repo, PYPROJECT, "pyproject")
    git(repo, "switch", "-c", "change")
    commit_pyproject(repo, PYPROJECT.replace('version = "1.0"', 'version = "1.1"'), "version")
    git(repo, "switch", "dev")
    commit_pyproject(repo, PYPROJECT.replace('"pyyaml>=6"', '"pyyaml>=7"'), "dependency on dev")
    git(repo, "switch", "change")
    assert run_main(repo, monkeypatch, tmp_path, "pull_request", base="dev") == PYPROJECT_SELECTED


def test_a_source_change_selects_the_tests_that_import_it(repo):
    assert decide(repo, ["hooks/sample.py"]) == ("selected", [["tests/test_sample.py"]])


def test_a_source_change_selects_recorded_runtime_dependents(repo):
    write(
        repo,
        {
            "hooks/runtime.py": "from hooks.sample import VALUE\nDEADLINE = VALUE + 1\n",
            "tests/test_runtime.py": "from hooks.runtime import DEADLINE\n",
        },
    )
    manifest = {
        "units": ["hooks/sample.py::<module>", "hooks/runtime.py::<module>"],
        "tests": {
            "tests/test_runtime.py::TestRuntime::test_deadline": {"units": [0, 1]},
            "tests/test_other.py::test_unrelated": {"units": [1]},
        },
    }
    scope, legs = ci_select.decide(repo, ["hooks/sample.py"], {}, 3, manifest=manifest)
    assert scope == "selected"
    assert {path for leg in legs for path in leg} == {"tests/test_sample.py", "tests/test_runtime.py"}


def test_selected_tests_expose_a_planted_runtime_dependency_failure(repo):
    write(
        repo,
        {
            "hooks/__init__.py": "",
            "hooks/sample.py": "VALUE = 2\n",
            "hooks/runtime.py": "from hooks.sample import VALUE\nDEADLINE = VALUE + 1\n",
            "tests/test_sample.py": "from hooks.sample import VALUE\ndef test_positive(): assert VALUE > 0\n",
            "tests/test_runtime.py": "from hooks.runtime import DEADLINE\ndef test_deadline(): assert DEADLINE == 2\n",
        },
    )
    manifest = {
        "units": ["hooks/sample.py::<module>"],
        "tests": {"tests/test_runtime.py::test_deadline": {"units": [0]}},
    }
    outcomes = []
    for recorded in (None, manifest):
        scope, legs = ci_select.decide(repo, ["hooks/sample.py"], {}, 12, manifest=recorded)
        assert scope == "selected"
        files = [path for leg in legs for path in leg]
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import runpy, sys; [test() for path in sys.argv[1:] for name, test in runpy.run_path(path).items() "
                "if name.startswith('test_') and callable(test)]",
                *files,
            ],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        outcomes.append((len(files), result.returncode))
        if recorded is not None:
            assert "AssertionError" in result.stderr
    assert outcomes == [(1, 0), (2, 1)]


def test_recorded_dependents_still_use_parallel_selected_legs(repo):
    write(repo, {"tests/test_runtime.py": "import os\n", "tests/test_deleted.py": "import os\n"})
    (repo / "tests/test_deleted.py").unlink()
    manifest = {
        "units": ["hooks/sample.py::deadline"],
        "tests": {
            "tests/test_runtime.py::test_deadline[first]": {"units": [0]},
            "tests/test_runtime.py::test_deadline[second]": {"units": [0]},
            "tests/test_deleted.py::test_deadline": {"units": [0]},
            "tests/test bad.py::test_deadline": {"units": [0]},
        },
    }
    scope, legs = ci_select.decide(
        repo,
        ["hooks/sample.py"],
        seconds(test_sample=ci_select.LEG_SECONDS, test_runtime=ci_select.LEG_SECONDS),
        12,
        manifest=manifest,
    )
    assert scope == "selected"
    assert sorted(legs) == [["tests/test_runtime.py"], ["tests/test_sample.py"]]


def test_main_reads_the_same_recorded_coverage_as_mutation(repo, monkeypatch, tmp_path, capsys):
    write(repo, {"tests/test_runtime.py": "import os\n"})
    git(repo, "add", "tests/test_runtime.py")
    git(repo, "commit", "-m", "runtime baseline")
    write(repo, {"hooks/sample.py": "VALUE = 2\n"})
    git(repo, "add", "hooks/sample.py")
    git(repo, "commit", "-m", "change")
    monkeypatch.setenv("CI_SELECT_REPOSITORY", "owner/repo")
    manifest = {
        "units": ["hooks/sample.py::<module>"],
        "tests": {"tests/test_runtime.py::test_deadline": {"units": [0]}},
    }
    calls = []

    def now(zone):
        assert zone is UTC
        return datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

    monkeypatch.setattr(ci_select, "datetime", SimpleNamespace(now=now))

    def record(repository, before):
        calls.append(repository)
        assert before == "2026-10-25T12:00:00Z"
        return {"id": 123}, manifest

    monkeypatch.setattr(ci_select, "find_record", record)
    output = run_main(repo, monkeypatch, tmp_path, "pull_request")
    assert calls == ["owner/repo"]
    assert "scope=selected" in output
    assert "tests/test_runtime.py" in output
    assert "Recorded coverage from dev run 123" in capsys.readouterr().out


def test_a_missing_record_preserves_import_selection(repo, monkeypatch, tmp_path):
    write(repo, {"hooks/sample.py": "VALUE = 2\n"})
    git(repo, "add", "hooks/sample.py")
    git(repo, "commit", "-m", "change")
    monkeypatch.setenv("CI_SELECT_REPOSITORY", "owner/repo")
    monkeypatch.setattr(ci_select, "find_record", lambda *_: None)
    output = run_main(repo, monkeypatch, tmp_path, "pull_request")
    assert "scope=selected" in output
    assert "files_1=tests/test_sample.py\n" in output


@pytest.mark.parametrize("path", ["docs/notes.md", "README.md"])
def test_main_does_not_fetch_coverage_for_docs_only_changes(repo, monkeypatch, tmp_path, path):
    write(repo, {path: "{}"})
    git(repo, "add", path)
    git(repo, "commit", "-m", "change")
    monkeypatch.setenv("CI_SELECT_REPOSITORY", "owner/repo")

    def record(*_):
        pytest.fail("Coverage lookup is unnecessary for a docs only change")

    monkeypatch.setattr(ci_select, "find_record", record)
    run_main(repo, monkeypatch, tmp_path, "pull_request")


def test_the_workflow_supplies_recorded_coverage_access():
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["select"]
    select = next(step for step in job["steps"] if step.get("name") == "Select the tests the change reaches")
    assert select["env"]["CI_SELECT_REPOSITORY"] == "${{ github.repository }}"
    assert select["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token || github.token }}"
    assert job["permissions"]["actions"] == "read"
    token = next(step for step in job["steps"] if step.get("id") == "app-token")
    assert (
        "github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name == github.repository"
        in token["if"]
    )


@pytest.mark.parametrize(
    ("fork", "actor", "expected"),
    [(False, "nestorcolt", True), (True, "nestorcolt", False), (False, "dependabot[bot]", False)],
)
def test_recorded_coverage_token_supports_unprivileged_pull_requests(fork, actor, expected):
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["select"]
    token = next(step for step in job["steps"] if step.get("id") == "app-token")
    condition = token["if"].replace("&&", "and").replace("||", "or")
    values = {
        "github.event_name": "pull_request",
        "github.event.pull_request.head.repo.full_name": "fork/repo" if fork else "owner/repo",
        "github.repository": "owner/repo",
        "github.actor": actor,
    }
    for name in sorted(values, key=len, reverse=True):
        condition = condition.replace(name, repr(values[name]))
    assert eval(condition) is expected
    select = next(step for step in job["steps"] if step.get("id") == "select")
    assert '"$RUNNER_TEMP/provenance-python/bin/python" -m scripts.ci_select' in select["run"]


def test_a_changed_test_file_selects_itself(repo):
    assert decide(repo, ["tests/test_other.py"]) == ("selected", [["tests/test_other.py"]])


def test_a_changed_file_outside_python_selects_the_tests_that_name_it(repo):
    write(repo, {"scripts/static/ledger.js": "x\n"})
    assert decide(repo, ["scripts/static/ledger.js"]) == ("selected", [["tests/test_page.py"]])


@pytest.mark.parametrize("source", ["scripts/static/page.css", "outside/page.py"])
def test_other_sources_do_not_select_python_tests_by_stem(repo, source):
    write(repo, {source: "x\n"})
    assert ci_select.selected_tests(repo, [source], False) == set()


def test_a_changed_file_is_also_selected_by_its_full_path(repo):
    write(repo, {"tests/test_docs.py": "DOC = 'docs/guide/index.md'\n"})
    assert decide(repo, ["docs/guide/index.md"]) == ("selected", [["tests/test_docs.py"]])


@pytest.mark.parametrize(
    "changed",
    [
        ".github/workflows/new.yml",
        ".github/workflows/new.yaml",
        ".github/runner-classes.json",
        ".github/workflows/trusted-actors.json",
        "scripts/ci_runner_classes.py",
        "scripts/ci_runner_trust.py",
    ],
)
def test_workflow_and_runner_changes_select_routing_contracts(repo, changed):
    contracts = {
        "tests/test_ci_anton_runners.py",
        "tests/test_ci_runner_classes.py",
        "tests/test_ci_runner_trust.py",
    }
    write(repo, {path: "import os\n" for path in contracts})
    scope, legs = decide(repo, [changed])
    assert scope == "selected"
    assert set().union(*map(set, legs)) == contracts


JOBS_WORKFLOW = """\
name: Tests
on:
  pull_request: {}
env:
  LEVEL: "1"
jobs:
  lint:
    runs-on: anton
    steps: &checks
      - run: ruff check
  docs:
    runs-on: anton
    steps: *checks
  unit:
    runs-on: anton
    steps:
      - run: pytest
"""
UNIT_EDIT = JOBS_WORKFLOW.replace("run: pytest", "run: pytest -x")
CHECKS_EDIT = JOBS_WORKFLOW.replace("run: ruff check", "run: ruff check .")
LOADED = "W = yaml.safe_load(open('.github/workflows/test.yml'))\n\n\n"
NAMED = """\
WORKFLOW = '.github/workflows/test.yml'
W = {}


def test_lint():
    assert W['jobs']['lint']


def test_docs():
    assert W['jobs']['docs']


def test_every():
    assert all(W['jobs'].values())
"""
UNNAMED = """\
JOBS = load()['jobs']


def test_opened():
    pass


def test_outside():
    pass


def test_unread():
    pass


def test_docs():
    assert JOBS['docs']
"""
READERS = {
    "tests/test_named.py": NAMED,
    "tests/test_unnamed.py": UNNAMED,
    "tests/test_basename.py": "SPECS = {'test.yml': {}}\n\n\ndef test_unit():\n    assert SPECS['test.yml']['jobs']['unit']\n",
}
RECORD = {
    "units": [],
    "tests": {
        "tests/test_named.py::test_lint": {"units": [], "files": [ci_select.WORKFLOW]},
        "tests/test_unnamed.py::test_opened[first]": {"units": [], "files": [ci_select.WORKFLOW]},
        "tests/test_unnamed.py::test_outside": {"units": [], "files": [], "outside": [ci_select.WORKFLOW]},
        "tests/test_unnamed.py::test_unread": {"units": [], "files": ["README.md"]},
        "tests/test_gone.py::test_gone": {"units": [], "files": [ci_select.WORKFLOW]},
    },
}
LINT_READERS = ["tests/test_named.py::test_every", "tests/test_named.py::test_lint"]
UNNAMED_LINT_READERS = [
    "tests/test_unnamed.py::test_opened",
    "tests/test_unnamed.py::test_outside",
    "tests/test_unnamed.py::test_unread",
]
PAIR = "def test_a():\n    pass\n\n\ndef test_b():\n    pass\n"
INVENTORY = """\
import yaml


def test_inventory():
    workflow = yaml.safe_load(open('.github/workflows/test.yml'))
    workflow['jobs']['unit']['needs'] = []
    assert normalize({'test.yml': workflow})
"""


def commit_workflow(repo: Path, text: str, message: str) -> None:
    write(repo, {ci_select.WORKFLOW: text})
    git(repo, "add", ci_select.WORKFLOW)
    git(repo, "commit", "-m", message)


@pytest.mark.feature("ci-select-workflow-job-readers")
@pytest.mark.parametrize(
    ("edited", "jobs"),
    [
        (UNIT_EDIT, {"unit"}),
        (CHECKS_EDIT, {"lint", "docs"}),
        (JOBS_WORKFLOW.replace("  unit:\n", "  smoke:\n    runs-on: anton\n  unit:\n"), {"smoke"}),
        (JOBS_WORKFLOW.replace("  docs:\n    runs-on: anton\n    steps: *checks\n", ""), {"docs"}),
        (JOBS_WORKFLOW.replace("name: Tests\n", "# reviewed\nname: Tests\n"), set()),
    ],
    ids=["one-job", "anchor", "added-job", "removed-job", "comment"],
)
def test_changed_jobs_names_the_jobs_whose_parsed_definition_changed(repo: Path, edited: str, jobs: set[str]) -> None:
    commit_workflow(repo, JOBS_WORKFLOW, "workflow")
    commit_workflow(repo, edited, "edit")
    assert ci_select.changed_jobs(repo, "HEAD~1", "HEAD") == jobs


@pytest.mark.feature("ci-select-workflow-job-readers")
@pytest.mark.parametrize(
    ("before", "after"),
    [
        (JOBS_WORKFLOW, UNIT_EDIT.replace('LEVEL: "1"', 'LEVEL: "2"')),
        (JOBS_WORKFLOW, UNIT_EDIT.replace("pull_request: {}", "push: {}")),
        (None, UNIT_EDIT),
        (JOBS_WORKFLOW, UNIT_EDIT.replace("name: Tests", "name: [Tests")),
        (JOBS_WORKFLOW, UNIT_EDIT.replace("jobs:\n", "concurrency: tests\njobs:\n")),
        (JOBS_WORKFLOW, JOBS_WORKFLOW.partition("jobs:")[0] + "jobs:\n  - lint\n"),
        (JOBS_WORKFLOW, JOBS_WORKFLOW.partition("jobs:")[0] + "jobs:\n"),
        ("name: Tests\n", "name: Tests\n# reviewed\n"),
    ],
    ids=["env", "trigger", "new-workflow", "unparsed", "added-key", "jobs-list", "null-jobs", "no-jobs"],
)
def test_changed_jobs_is_none_outside_the_jobs_or_without_two_parsed_workflows(
    repo: Path, before: str | None, after: str
) -> None:
    if before is not None:
        commit_workflow(repo, before, "workflow")
    commit_workflow(repo, after, "edit")
    assert ci_select.changed_jobs(repo, "HEAD~1", "HEAD") is None


@pytest.mark.feature("ci-select-workflow-job-readers")
def test_changed_jobs_compares_the_head_against_its_merge_base(repo: Path) -> None:
    commit_workflow(repo, JOBS_WORKFLOW, "workflow")
    git(repo, "switch", "-c", "change")
    commit_workflow(repo, UNIT_EDIT, "unit")
    git(repo, "switch", "dev")
    commit_workflow(repo, CHECKS_EDIT, "checks on dev")
    git(repo, "switch", "change")
    assert ci_select.changed_jobs(repo, "dev", "HEAD") == {"unit"}


@pytest.mark.feature("ci-select-workflow-job-readers")
@pytest.mark.parametrize(
    ("body", "read"),
    [
        ("def test_a():\n    assert W['jobs']['lint']\n", {"lint"}),
        ("def test_a():\n    assert W.get('jobs')['lint'] and W['jobs'].get('docs')\n", {"lint", "docs"}),
        ("def test_a():\n    assert 'lint' in W['jobs'] and W['jobs'].pop('unit')\n", {"lint", "unit"}),
        ("def test_a():\n    jobs = W['jobs']\n    assert jobs['lint']\n", {"lint"}),
        ("def jobs():\n    return W['jobs']\n\n\ndef test_a():\n    assert jobs()['lint']\n", {"lint"}),
        ("JOBS = W['jobs']\n\n\ndef test_a():\n    assert JOBS['docs']\n", {"docs"}),
        ("def test_a():\n    print('jobs')\n    assert W['jobs']['lint']\n", {"lint"}),
        (
            "def lint():\n    jobs = W['jobs']\n    return jobs['lint']\n\n\n"
            "def other():\n    jobs = []\n    return sorted(jobs)\n\n\n"
            "def test_a():\n    assert lint() and other()\n",
            {"lint"},
        ),
        ("def test_a():\n    assert all(W['jobs'].values())\n", None),
        ("def test_a(name):\n    assert W['jobs'][name]\n", None),
        ("def test_a():\n    assert W['jobs'][0]\n", None),
        ("def test_a():\n    assert len(W['jobs'])\n", None),
        ("def test_a():\n    assert 'docs' != W['jobs']\n", None),
        ("def test_a():\n    assert W\n", None),
        ("def test_a():\n    jobs = W['jobs']\n", None),
        (
            "def jobs():\n    return W['jobs']\n\n\ndef test_a():\n    assert list(map(jobs, [1])) and jobs()['lint']\n",
            None,
        ),
        ("def every():\n    return dict(W['jobs'].items())\n\n\ndef test_a():\n    assert every()['lint']\n", None),
        (
            "@pytest.fixture\ndef jobs():\n    return W['jobs']\n\n\n"
            "def test_a(jobs):\n    assert jobs.get('lint') and W['jobs']['docs']\n",
            None,
        ),
        ("def test_a():\n    assert W['jobs']['lint'] and normalize({'test.yml': W})\n", None),
        (
            "def test_a():\n    workflow = yaml.safe_load(TEXT)\n    jobs = workflow['jobs']\n"
            "    assert jobs['lint'] and normalize({'test.yml': workflow})\n",
            None,
        ),
        ("def test_a():\n    assert W['on'] and 'env' in W and W.get('jobs')['lint']\n", {"lint"}),
        ("def test_a():\n    W['jobs']['lint']['needs'] = []\n    assert yaml.safe_dump(W)\n", None),
        (
            "def workflow():\n    return yaml.safe_load(TEXT)\n\n\ndef test_a():\n    assert workflow()['jobs']['docs']\n",
            {"docs"},
        ),
        (
            "def workflow():\n    return yaml.safe_load(TEXT)\n\n\n"
            "def test_a():\n    assert workflow()['jobs']['docs'] and normalize(workflow())\n",
            None,
        ),
        ("def test_a():\n    assert W['jobs']['lint'] and W.items()\n", None),
        (
            "def test_a():\n    doc = load_workflow()\n    assert doc['jobs']['lint'] and normalize({'test.yml': doc})\n",
            None,
        ),
        ("def test_a():\n    doc = load_workflow()\n    assert doc['jobs']['lint'] and doc['on']\n", {"lint"}),
        ("def test_a():\n    assert load_workflow()['jobs']['lint'] and normalize(load_workflow())\n", None),
        ("def test_a(workflow):\n    assert workflow.get('jobs')['lint'] and normalize(workflow)\n", None),
        ("def test_a():\n    doc = safe_load(TEXT)\n    assert doc['jobs']['lint'] and normalize(doc)\n", None),
        (
            "def test_a():\n    doc = {'jobs': {'lint': {}}}\n    assert doc['jobs']['lint'] and normalize(doc)\n",
            {"lint"},
        ),
        (
            "def workflow():\n    return {'jobs': {}}\n\n\n"
            "def pair():\n    base, head = workflow(), workflow()\n    return base, head\n\n\n"
            "def test_a():\n    base, head = pair()\n    head = copy.deepcopy(base)\n"
            "    assert head['jobs']['lint'] and grade(base, head)\n",
            {"lint"},
        ),
        (
            "def workflow():\n    return load_workflow()\n\n\n"
            "def test_a():\n    doc = workflow()\n    assert doc['jobs']['lint'] and normalize(doc)\n",
            None,
        ),
        (
            "def test_a():\n    doc = MAPPING['workflow']\n    assert doc['jobs']['lint'] and normalize(doc)\n",
            None,
        ),
        (
            "def workflow():\n    return workflow()\n\n\n"
            "def test_a():\n    doc = workflow()\n    assert doc['jobs']['lint'] and normalize(doc)\n",
            None,
        ),
        (
            "def test_a():\n    doc = {'jobs': {}}\n    doc = dict(doc)\n"
            "    assert doc['jobs']['lint'] and normalize(doc)\n",
            None,
        ),
        (
            "def test_a():\n    doc = load_workflow({'jobs': {}})\n    assert doc['jobs']['lint'] and normalize(doc)\n",
            None,
        ),
        (
            "BASE = {'jobs': {}}\n\n\n"
            "def test_a():\n    assert copy.deepcopy(BASE)['jobs']['lint'] and normalize(copy.deepcopy(BASE))\n",
            {"lint"},
        ),
        (
            "def test_a():\n    assert load_workflow()['jobs']['lint'] and len(other())\n",
            {"lint"},
        ),
        (
            "def helper():\n    doc = load_workflow()\n    return normalize(doc)\n\n\n"
            "def test_a():\n    doc = {'jobs': {}}\n    assert doc['jobs']['lint'] and helper()\n",
            {"lint"},
        ),
        (
            "def helper(doc):\n    return normalize(doc)\n\n\n"
            "def test_a(doc):\n    assert doc['jobs']['lint'] and helper(other)\n",
            {"lint"},
        ),
        (
            "def test_a():\n    first = yaml.safe_load(TEXT)\n    second = yaml.safe_load(TEXT)\n"
            "    assert first['jobs']['lint'] and normalize(second)\n",
            None,
        ),
        (
            "def test_a(bundle):\n    assert bundle['workflow']['jobs']['lint'] and normalize(bundle['workflow'])\n",
            None,
        ),
        (
            "def test_a(bundle):\n    assert bundle.workflow['jobs']['lint'] and normalize(bundle.workflow)\n",
            None,
        ),
        (
            "def test_a():\n    doc = {**load_workflow()}\n    assert doc['jobs']['lint'] and normalize(doc)\n",
            None,
        ),
        (
            "def test_a():\n    doc = {key: value for key, value in load_workflow().items()}\n"
            "    assert doc['jobs']['lint'] and normalize(doc)\n",
            None,
        ),
        ("def test_a():\n    assert [doc['jobs']['lint'] for doc in yaml.safe_load_all(TEXT)]\n", None),
    ],
    ids=[
        "index",
        "get",
        "membership-and-pop",
        "assigned",
        "returned",
        "module-constant",
        "other-call",
        "own-scope",
        "iterated",
        "variable-key",
        "integer-key",
        "passed-on",
        "compared",
        "unindexed",
        "unread-name",
        "referenced-helper",
        "returned-after-iteration",
        "fixture",
        "document-handed-on",
        "loaded-document-handed-on",
        "document-other-keys",
        "document-dumped",
        "returned-document",
        "returned-document-handed-on",
        "document-items",
        "helper-document-handed-on",
        "helper-document-indexed",
        "called-document-handed-on",
        "fixture-document-handed-on",
        "aliased-load-handed-on",
        "built-document-handed-on",
        "built-copy-handed-on",
        "module-helper-external-document",
        "subscript-external-document",
        "recursive-document-helper",
        "recursive-document-binding",
        "single-argument-external-loader",
        "direct-copy-built-document",
        "unrelated-call-beside-document",
        "same-local-name-in-helper",
        "same-parameter-name-in-helper",
        "second-loaded-document-handed-on",
        "fixture-subscript-document-handed-on",
        "fixture-attribute-document-handed-on",
        "external-dictionary-unpacking",
        "external-dictionary-comprehension",
        "loaded-all",
    ],
)
def test_definition_jobs_reads_the_jobs_a_test_indexes_by_name(body: str, read: set[str] | None) -> None:
    assert ci_select.definition_jobs(LOADED + body) == {"test_a": read}


@pytest.mark.feature("ci-select-workflow-job-readers")
def test_definition_jobs_covers_only_test_functions_and_classes() -> None:
    source = LOADED + (
        "def helper():\n    return W['jobs']['unit']\n\n\n"
        "class TestBox:\n    def test_a(self):\n        assert W['jobs']['lint']\n\n\n"
        "class Box:\n    pass\n\n\n"
        "async def test_b():\n    assert W['jobs']['docs']\n\n\n"
        "if W:\n\n    def test_c():\n        assert W['jobs']['unit']\n"
    )
    assert ci_select.definition_jobs(source) == {"TestBox": {"lint"}, "test_b": {"docs"}, "test_c": None}


@pytest.mark.feature("ci-select-workflow-job-readers")
def test_workflow_tests_offers_the_job_readers_of_each_named_or_recorded_file(repo: Path) -> None:
    write(repo, READERS)
    source = "def test_a():\n    assert W['jobs']['lint']\n"
    write(repo, dict.fromkeys(("tests/test_files.py", "tests/test_outside.py", "tests/test_other.py"), source))
    manifest = {
        "tests": {
            **RECORD["tests"],
            "tests/test_files.py::test_a": {"files": [ci_select.WORKFLOW]},
            "tests/test_outside.py::test_a": {"outside": [ci_select.WORKFLOW]},
            "tests/test_other.py::test_a": {"files": ["README.md"], "outside": []},
        }
    }
    assert ci_select.workflow_tests(repo, {"lint"}, manifest) == {
        *LINT_READERS,
        *UNNAMED_LINT_READERS,
        "tests/test_files.py::test_a",
        "tests/test_outside.py::test_a",
    }


@pytest.mark.feature("ci-select-workflow-job-readers")
def test_workflow_tests_without_a_record_offers_each_definition_of_a_file_naming_the_workflow(repo: Path) -> None:
    write(repo, READERS)
    assert ci_select.workflow_tests(repo, {"unit"}, None) == {
        "tests/test_basename.py::test_unit",
        "tests/test_named.py::test_every",
    }


@pytest.mark.feature("ci-select-workflow-job-readers")
def test_a_one_job_edit_selects_that_jobs_readers_and_the_every_job_readers(repo: Path) -> None:
    write(repo, READERS)
    assert ci_select.decide(repo, [ci_select.WORKFLOW], {}, 3, ci_select.Change(jobs={"lint"})) == (
        "selected",
        [LINT_READERS],
    )


@pytest.mark.feature("ci-select-workflow-job-readers")
def test_an_edit_outside_the_jobs_selects_every_workflow_test(repo: Path) -> None:
    write(repo, READERS)
    assert decide(repo, [ci_select.WORKFLOW]) == ("selected", [["tests/test_basename.py", "tests/test_named.py"]])


@pytest.mark.feature("ci-select-workflow-job-readers")
def test_a_recorded_workflow_reader_file_offers_each_definition_that_reads_a_changed_job(repo: Path) -> None:
    write(repo, READERS)
    assert ci_select.decide(repo, [ci_select.WORKFLOW], {}, 3, ci_select.Change(jobs={"lint"}), RECORD) == (
        "selected",
        [[*LINT_READERS, *UNNAMED_LINT_READERS]],
    )


@pytest.mark.feature("ci-select-workflow-job-readers")
@pytest.mark.parametrize(
    ("source", "job"),
    [
        (INVENTORY, "lint"),
        (
            "WORKFLOW = '.github/workflows/test.yml'\n"
            "def test_a(bundle):\n    assert bundle['workflow']['jobs']['lint'] and normalize(bundle['workflow'])\n",
            "docs",
        ),
        (
            "WORKFLOW = '.github/workflows/test.yml'\n"
            "def test_a(bundle):\n    assert bundle.workflow['jobs']['lint'] and normalize(bundle.workflow)\n",
            "docs",
        ),
        (
            "WORKFLOW = '.github/workflows/test.yml'\n"
            "def test_a():\n    doc = {**load_workflow()}\n    assert doc['jobs']['lint'] and normalize(doc)\n",
            "docs",
        ),
        (
            "WORKFLOW = '.github/workflows/test.yml'\n"
            "def test_a():\n    doc = {key: value for key, value in load_workflow().items()}\n"
            "    assert doc['jobs']['lint'] and normalize(doc)\n",
            "docs",
        ),
    ],
    ids=["named-document", "subscript-document", "attribute-document", "unpacked-document", "comprehended-document"],
)
def test_a_document_handed_on_beside_a_job_index_selects_its_reader_for_any_job_edit(
    repo: Path, source: str, job: str
) -> None:
    write(repo, {"tests/test_inventory.py": source})
    assert ci_select.decide(repo, [ci_select.WORKFLOW], {}, 3, ci_select.Change(jobs={job})) == (
        "selected",
        [["tests/test_inventory.py"]],
    )


@pytest.mark.feature("ci-select-workflow-job-readers")
def test_a_job_edit_beside_another_change_keeps_the_tests_naming_that_change(repo: Path) -> None:
    write(repo, {**READERS, "tests/test_lonely.py": "PATH = 'scripts/lonely.py'\n"})
    scope, legs = ci_select.decide(
        repo, [ci_select.WORKFLOW, "scripts/lonely.py"], {}, 3, ci_select.Change(jobs={"lint"})
    )
    assert scope == "selected"
    assert sorted(entry for leg in legs for entry in leg) == ["tests/test_lonely.py", *LINT_READERS]


@pytest.mark.feature("ci-select-workflow-job-readers")
def test_a_selected_definition_in_a_fakeredis_module_selects_the_suite_speed_check(repo: Path) -> None:
    write(repo, {**READERS, "tests/test_named.py": "import fakeredis\n" + NAMED, ci_select.SUITE_SPEED: "import os\n"})
    assert ci_select.decide(repo, [ci_select.WORKFLOW], {}, 3, ci_select.Change(jobs={"docs"})) == (
        "selected",
        [["tests/test_named.py::test_docs", "tests/test_named.py::test_every", ci_select.SUITE_SPEED]],
    )


@pytest.mark.feature("ci-select-workflow-job-readers")
def test_selected_definitions_of_a_splittable_file_run_apart_and_count_their_seconds(tmp_path: Path) -> None:
    write(tmp_path, {"tests/test_pair.py": PAIR})
    durations = {"tests/test_pair.py::test_a": 200.0, "tests/test_pair.py::test_b": 200.0}
    legs = ci_select.legs_for(tmp_path, {"tests/test_pair.py::test_a", "tests/test_pair.py::test_b"}, durations, 4)
    assert sorted(legs) == [["tests/test_pair.py::test_a"], ["tests/test_pair.py::test_b"]]


@pytest.mark.feature("ci-select-workflow-job-readers")
@pytest.mark.parametrize(
    ("tests", "legs"),
    [
        ({"tests/test_pair.py::test_a"}, [["tests/test_pair.py::test_a"]]),
        (
            {"tests/test_pair.py::test_a", "tests/test_pair.py::test_b"},
            [["tests/test_pair.py::test_a", "tests/test_pair.py::test_b"]],
        ),
        ({"tests/test_single.py::test_a"}, [["tests/test_single.py"]]),
        ({"tests/test_pair.py", "tests/test_pair.py::test_a"}, [["tests/test_pair.py"]]),
        ({"tests/test_pair.py::test_gone"}, [["tests/test_pair.py"]]),
        ({"tests/routing/test_codex_api.py::test_a"}, [["tests/routing/test_codex_api.py"]]),
    ],
    ids=[
        "part-of-a-splittable-file",
        "all-definitions-without-durations",
        "unsplittable-file",
        "whole-file-wins",
        "unknown-definition",
        "first-shard-file",
    ],
)
def test_a_definition_runs_alone_only_when_its_file_runs_its_definitions_apart(
    tmp_path: Path, tests: set[str], legs: list[list[str]]
) -> None:
    write(
        tmp_path,
        {
            "tests/test_pair.py": PAIR,
            "tests/test_single.py": "def test_a():\n    pass\n",
            "tests/routing/test_codex_api.py": PAIR,
        },
    )
    assert ci_select.legs_for(tmp_path, tests, {}, 3) == legs


@pytest.mark.feature("ci-select-workflow-job-readers")
def test_main_selects_the_readers_of_the_jobs_a_pull_request_changed(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write(repo, READERS)
    commit_workflow(repo, JOBS_WORKFLOW, "workflow")
    commit_workflow(repo, UNIT_EDIT, "unit")
    assert run_main(repo, monkeypatch, tmp_path, "pull_request") == (
        "scope=selected\nlegs=[1]\ncount=1\nfiles_1=tests/test_basename.py tests/test_named.py::test_every\n"
    )


@pytest.mark.parametrize("changed", [*locked_tests.LOCKED, locked_tests.MANIFEST.as_posix()])
def test_locked_test_and_manifest_changes_select_the_manifest_agreement_test(repo, changed):
    write(repo, {"tests/test_locked_tests.py": "import os\n", changed: "VALUE = 1\n"})
    assert "tests/test_locked_tests.py" in ci_select.selected_tests(repo, [changed], False)


def test_a_python_source_is_not_selected_by_its_bare_file_name(repo):
    write(repo, {"tests/test_names.py": "NAME = 'lonely.py'\n"})
    assert decide(repo, ["scripts/lonely.py"]) == ("full", [])


def test_a_deleted_test_file_is_not_selected(repo):
    assert decide(repo, ["tests/test_gone.py"]) == ("full", [])


def test_a_changed_test_helper_selects_the_tests_that_import_it(repo):
    write(repo, {"tests/helpers.py": "VALUE = 1\n", "tests/test_helped.py": "from tests import helpers\n"})
    assert decide(repo, ["tests/helpers.py"]) == ("selected", [["tests/test_helped.py"]])


def test_a_nested_test_that_names_a_changed_file_is_selected(repo):
    write(repo, {"tests/swarm/test_nested.py": "PAGE = 'ledger.js'\n", "scripts/static/ledger.js": "x\n"})
    assert decide(repo, ["scripts/static/ledger.js"]) == (
        "selected",
        [["tests/swarm/test_nested.py", "tests/test_page.py"]],
    )


SUITE_SPEED = "tests/test_suite_speed.py"
UNREGISTERED_FAKEREDIS = "import fakeredis\nfrom hooks import sample\n"


@pytest.mark.parametrize(
    "changed",
    [
        ["tests/test_store.py"],
        ["tests/test_other.py"],
        ["tests/test_gone.py", "tests/test_other.py"],
        ["tests/swarm/test_plain.py"],
    ],
)
def test_a_changed_test_module_selects_the_suite_speed_checks(repo, changed):
    write(repo, {SUITE_SPEED: "import os\n", "tests/test_store.py": UNREGISTERED_FAKEREDIS})
    write(repo, {"tests/swarm/test_plain.py": "import os\n"})
    scope, legs = decide(repo, changed)
    assert scope == "selected"
    assert SUITE_SPEED in set().union(*map(set, legs))


def test_a_source_a_fakeredis_test_imports_selects_the_suite_speed_checks(repo):
    write(repo, {SUITE_SPEED: "import os\n", "tests/test_store.py": UNREGISTERED_FAKEREDIS})
    scope, legs = decide(repo, ["hooks/sample.py"])
    assert scope == "selected"
    assert set().union(*map(set, legs)) == {SUITE_SPEED, "tests/test_sample.py", "tests/test_store.py"}


def test_a_source_no_fakeredis_test_imports_leaves_the_suite_speed_checks_out(repo):
    write(repo, {SUITE_SPEED: "import os\n"})
    assert decide(repo, ["hooks/sample.py"]) == ("selected", [["tests/test_sample.py"]])


def test_a_changed_file_outside_python_leaves_the_suite_speed_checks_out(repo):
    write(repo, {SUITE_SPEED: "import os\n", "scripts/static/ledger.js": "x\n"})
    assert decide(repo, ["scripts/static/ledger.js"]) == ("selected", [["tests/test_page.py"]])


def test_a_test_module_whose_name_breaks_the_output_line_still_runs_the_full_suite(repo):
    write(repo, {SUITE_SPEED: "import os\n", "tests/test_spaced name.py": "import os\n"})
    assert decide(repo, ["tests/test_spaced name.py"]) == ("full", [])


@pytest.mark.parametrize("name", ["tests/test_spaced name.py", "tests/test_split\nscope=none.py"])
def test_a_test_file_whose_name_breaks_the_output_line_is_never_selected(repo, name):
    write(repo, {name: "from hooks import sample\n"})
    assert decide(repo, ["hooks/sample.py", name]) == ("selected", [["tests/test_sample.py"]])


def test_a_change_no_test_reaches_outside_the_sources_runs_nothing(repo):
    assert decide(repo, ["docs/unnamed.md"]) == ("none", [])


@pytest.mark.feature("ci-select-markdown-glob-tests")
@pytest.mark.parametrize("changed", [["docs/pillars/swarm.md", "docs/pillars/index.md"], ["README.md"]])
def test_a_markdown_only_change_selects_the_tests_that_glob_markdown(repo: Path, changed: list[str]) -> None:
    write(
        repo,
        {
            "tests/test_journal.py": "DOCS = sorted(ROOT.rglob('*.md'))\n",
            "tests/test_roles.py": 'PAGES = ROOT.glob("profiles/**/*.md")\n',
        },
    )
    assert decide(repo, changed) == ("selected", [["tests/test_journal.py", "tests/test_roles.py"]])


@pytest.mark.feature("ci-select-markdown-glob-tests")
def test_a_markdown_change_skips_tests_that_glob_other_files(repo: Path) -> None:
    write(
        repo,
        {
            "tests/test_journal.py": "DOCS = ROOT.rglob( '*.md' )\n",
            "tests/test_python.py": "FILES = ROOT.glob('*.py')\n",
            "tests/test_mdx.py": "FILES = ROOT.glob('*.mdx')\n",
            "tests/test_plain.py": "FILES = ROOT.glob('notes.md')\n",
        },
    )
    assert decide(repo, ["docs/x.md"]) == ("selected", [["tests/test_journal.py"]])


@pytest.mark.feature("ci-select-markdown-glob-tests")
def test_a_source_change_does_not_select_the_tests_that_glob_markdown(repo: Path) -> None:
    write(repo, {"tests/test_journal.py": "DOCS = sorted(ROOT.rglob('*.md'))\n"})
    assert decide(repo, ["hooks/sample.py"]) == ("selected", [["tests/test_sample.py"]])


CATALOGUE_TEST = "tests/test_docs_tick.py"
CATALOGUE_IMPORTS = "from scripts.tick import catalogue, registry\n"


@pytest.mark.feature("ci-select-tick-catalogue-page")
def test_a_changed_tick_step_module_selects_the_catalogue_page_test(repo: Path) -> None:
    write(
        repo,
        {
            CATALOGUE_TEST: CATALOGUE_IMPORTS,
            "scripts/tick/steps/report.py": "VALUE = 1\n",
            "tests/tick/test_report.py": "import scripts.tick.steps.report\n",
        },
    )
    assert decide(repo, ["scripts/tick/steps/report.py"]) == (
        "selected",
        [[CATALOGUE_TEST, "tests/tick/test_report.py"]],
    )


@pytest.mark.feature("ci-select-tick-catalogue-page")
def test_a_tick_step_module_no_test_reaches_still_runs_the_full_suite(repo: Path) -> None:
    write(repo, {CATALOGUE_TEST: CATALOGUE_IMPORTS, "scripts/tick/steps/lonely.py": "VALUE = 1\n"})
    assert decide(repo, ["scripts/tick/steps/lonely.py"]) == ("full", [])


@pytest.mark.feature("ci-select-tick-catalogue-page")
def test_a_tick_change_outside_the_step_package_adds_no_catalogue_page_test(repo: Path) -> None:
    write(
        repo,
        {
            CATALOGUE_TEST: CATALOGUE_IMPORTS,
            "scripts/tick/engine.py": "VALUE = 1\n",
            "tests/tick/test_engine.py": "import scripts.tick.engine\n",
        },
    )
    assert decide(repo, ["scripts/tick/engine.py"]) == ("selected", [["tests/tick/test_engine.py"]])


@pytest.mark.feature("ci-select-tick-catalogue-page")
def test_a_tree_without_the_catalogue_page_test_never_selects_it(repo: Path) -> None:
    write(
        repo,
        {
            "scripts/tick/steps/report.py": "VALUE = 1\n",
            "tests/tick/test_report.py": "import scripts.tick.steps.report\n",
        },
    )
    assert decide(repo, ["scripts/tick/steps/report.py"]) == ("selected", [["tests/tick/test_report.py"]])


@pytest.mark.feature("ci-select-tick-catalogue-page-edit")
def test_a_hand_edited_catalogue_page_selects_its_page_test(repo: Path) -> None:
    source = Path(__file__).with_name("test_docs_tick.py").read_text()
    write(repo, {CATALOGUE_TEST: source})
    assert decide(repo, ["docs/reference/tick-step-catalogue.md"]) == ("selected", [[CATALOGUE_TEST]])


@pytest.mark.parametrize("name", ["hooks/new.py", "scripts/static/new.js"])
def test_a_covered_source_no_test_reaches_runs_the_full_suite(repo, name):
    write(repo, {name: "x = 1\n"})
    assert decide(repo, [name]) == ("full", [])


@pytest.mark.parametrize("name", ["hooks/new.css", "lib/new.py", "scripts/ci_reuse_job.yml", "profiles/a/SKILL.txt"])
def test_an_unreached_file_that_is_not_documentation_runs_the_full_suite(repo, name):
    write(repo, {name: "x\n"})
    assert decide(repo, [name]) == ("full", [])


def test_a_deleted_source_no_test_names_runs_the_full_suite(repo):
    assert decide(repo, ["hooks/removed.py"]) == ("full", [])


@pytest.mark.parametrize(
    ("changed", "scope"),
    [
        (["docs/a.md", "README.md", "profiles/x/notes.md"], "none"),
        (["docs/a.md", "hooks/new.py"], "full"),
        (["hooks/new.py", "docs/a.md"], "full"),
        (["docs/image.png"], "none"),
    ],
)
def test_only_unreached_documentation_runs_nothing(repo, changed, scope):
    assert decide(repo, changed) == (scope, [])


def seconds(**files):
    return {f"tests/{name}.py::test_case": value for name, value in files.items()}


@pytest.fixture
def heavy(repo):
    write(repo, {f"tests/test_heavy{index}.py": "from hooks import sample\n" for index in range(1, 4)})
    return repo


def test_a_selection_at_its_leg_budget_still_runs_selected(heavy):
    budget = ci_select.LEG_SECONDS
    durations = seconds(test_sample=0.5, test_heavy1=budget, test_heavy2=budget, test_heavy3=budget - 0.5)
    scope, legs = decide(heavy, ["hooks/sample.py"], durations)
    assert scope == "selected"
    assert sorted(file for leg in legs for file in leg) == [
        "tests/test_heavy1.py",
        "tests/test_heavy2.py",
        "tests/test_heavy3.py",
        "tests/test_sample.py",
    ]
    assert len(legs) == 3


def test_a_selection_over_its_leg_budget_stays_selected_within_its_leg_limit(heavy):
    budget = ci_select.LEG_SECONDS
    durations = seconds(test_sample=1.0, test_heavy1=budget, test_heavy2=budget, test_heavy3=budget - 0.5)
    scope, legs = decide(heavy, ["hooks/sample.py"], durations)
    assert scope == "selected"
    assert len(legs) == 3
    assert sorted(file for leg in legs for file in leg) == [
        "tests/test_heavy1.py",
        "tests/test_heavy2.py",
        "tests/test_heavy3.py",
        "tests/test_sample.py",
    ]


def test_a_harness_change_runs_the_full_suite_however_large_the_selection(heavy):
    durations = seconds(test_sample=1.0, **{f"test_heavy{index}": 10 * ci_select.LEG_SECONDS for index in range(1, 4)})
    assert decide(heavy, ["hooks/sample.py", "pyproject.toml"], durations) == ("full", [])


@pytest.mark.parametrize(("total", "count"), [(1.0, 1), (180.0, 1), (180.5, 2), (360.0, 2), (360.5, 3)])
def test_a_selection_uses_one_leg_per_leg_budget_of_stored_time(heavy, total, count):
    durations = seconds(test_sample=total / 4, test_heavy1=total / 4, test_heavy2=total / 4, test_heavy3=total / 4)
    _, legs = decide(heavy, ["hooks/sample.py"], durations)
    assert len(legs) == count


def test_a_test_file_sums_the_stored_time_of_all_its_tests(heavy):
    durations = {"tests/test_heavy1.py::Case::test_a": 100.0, "tests/test_heavy1.py::Case::test_b": 100.0}
    _, legs = decide(heavy, ["hooks/sample.py"], durations)
    assert len(legs) == 2


def test_a_leg_with_no_file_is_dropped(repo):
    assert decide(repo, ["hooks/sample.py"], seconds(test_sample=200.0)) == ("selected", [["tests/test_sample.py"]])


def test_a_test_file_without_stored_time_counts_its_estimate(heavy):
    durations = seconds(test_heavy1=ci_select.LEG_SECONDS - 3 * ci_select.UNKNOWN_SECONDS + 1)
    _, legs = decide(heavy, ["hooks/sample.py"], durations)
    assert len(legs) == 2


def test_the_legs_balance_on_stored_time(heavy):
    durations = seconds(test_sample=10.0, test_heavy1=170.0, test_heavy2=150.0, test_heavy3=20.0)
    assert decide(heavy, ["hooks/sample.py"], durations) == (
        "selected",
        [["tests/test_heavy1.py", "tests/test_sample.py"], ["tests/test_heavy2.py", "tests/test_heavy3.py"]],
    )


def test_a_file_over_its_leg_share_splits_its_tests_across_legs(heavy):
    write(
        heavy,
        {
            "tests/test_heavy1.py": "from hooks import sample\ndef test_a(): pass\ndef test_b(): pass\nclass TestC:\n    def test_d(self): pass\n"
        },
    )
    durations = {
        "tests/test_heavy1.py::test_a[one]": 150.0,
        "tests/test_heavy1.py::test_a[two]": 50.0,
        "tests/test_heavy1.py::test_b": 180.0,
        "tests/test_heavy1.py::TestC::test_d": 170.0,
        **seconds(test_sample=10.0, test_heavy2=10.0, test_heavy3=10.0),
    }
    _, legs = decide(heavy, ["hooks/sample.py"], durations, limit=4)
    assert sorted(unit for leg in legs for unit in leg) == [
        "tests/test_heavy1.py::TestC",
        "tests/test_heavy1.py::test_a",
        "tests/test_heavy1.py::test_b",
        "tests/test_heavy2.py",
        "tests/test_heavy3.py",
        "tests/test_sample.py",
    ]
    assert len(legs) == 4
    assert all(sum(unit.startswith("tests/test_heavy1.py") for unit in leg) == 1 for leg in legs[:3])


def test_a_file_over_its_leg_share_with_an_imported_test_stays_whole(heavy):
    write(
        heavy,
        {
            "tests/test_heavy1.py": "from hooks import sample\nfrom tests.shared import test_made\ndef test_a(): pass\ndef test_b(): pass\n"
        },
    )
    durations = {f"tests/test_heavy1.py::{name}": 150.0 for name in ("test_a", "test_b", "test_made")}
    _, legs = decide(heavy, ["hooks/sample.py"], durations)
    assert ["tests/test_heavy1.py"] in legs


def test_a_split_file_keeps_its_new_tests(heavy):
    write(
        heavy,
        {
            "tests/test_heavy1.py": "from hooks import sample\ndef test_a(): pass\ndef test_b(): pass\ndef test_new(): pass\n"
        },
    )
    durations = {"tests/test_heavy1.py::test_a": 200.0, "tests/test_heavy1.py::test_b": 200.0}
    _, legs = decide(heavy, ["hooks/sample.py"], durations)
    units = {unit for leg in legs for unit in leg}
    assert "tests/test_heavy1.py::test_new" in units
    assert "tests/test_heavy1.py" not in units


@pytest.mark.parametrize(
    "source",
    [
        "class TestNew:\n    def test_c(self): pass\n",
        "@pytest.fixture\ndef test_value(): return 1\n",
        "from tests.shared import test_shared\n",
        "test_made = pytest.mark.unit(lambda: None)\n",
        "if True:\n    def test_nested(): pass\n",
        "from tests.shared import *\n",
        "import unittest\nclass Relay(unittest.TestCase):\n    def test_c(self): pass\n",
        "class TestInit:\n    def __init__(self): pass\n",
        "pytestmark = pytest.mark.xdist_group('shared')\n",
        "from tests.shared import TestShared\n",
    ],
)
def test_a_file_over_its_leg_share_with_an_unstored_test_pytest_may_skip_stays_whole(heavy, source):
    write(
        heavy,
        {
            "tests/test_heavy1.py": "import pytest\nfrom hooks import sample\ndef test_a(): pass\ndef test_b(): pass\n"
            + source
        },
    )
    durations = {"tests/test_heavy1.py::test_a": 200.0, "tests/test_heavy1.py::test_b": 200.0}
    _, legs = decide(heavy, ["hooks/sample.py"], durations)
    assert ["tests/test_heavy1.py"] in legs


def test_a_split_file_ignores_stored_tests_its_source_no_longer_has(heavy):
    write(heavy, {"tests/test_heavy1.py": "from hooks import sample\ndef test_a(): pass\ndef test_b(): pass\n"})
    durations = {f"tests/test_heavy1.py::{name}": 200.0 for name in ("test_a", "test_b", "test_gone")}
    _, legs = decide(heavy, ["hooks/sample.py"], durations)
    units = {unit for leg in legs for unit in leg}
    assert {"tests/test_heavy1.py::test_a", "tests/test_heavy1.py::test_b"} <= units
    assert not any("test_gone" in unit or unit == "tests/test_heavy1.py" for unit in units)


@pytest.mark.parametrize(
    "header",
    ["class TestInit:\n    def __init__(self): pass\n", "class TestInit(metaclass=type):\n"],
)
def test_a_file_over_its_leg_share_with_a_stored_class_pytest_may_refuse_stays_whole(heavy, header):
    write(
        heavy,
        {
            "tests/test_heavy1.py": "from hooks import sample\ndef test_a(): pass\n"
            + header
            + "    def test_b(self): pass\n"
        },
    )
    durations = {"tests/test_heavy1.py::test_a": 200.0, "tests/test_heavy1.py::TestInit::test_b": 200.0}
    _, legs = decide(heavy, ["hooks/sample.py"], durations)
    assert ["tests/test_heavy1.py"] in legs


def test_a_split_file_sums_each_top_level_test_over_its_nodes(tmp_path):
    write(
        tmp_path,
        {
            "tests/test_heavy1.py": "import pytest\ndef test_a(): pass\n@pytest.mark.unit\ndef test_b(): pass\n"
            "class TestC:\n    flag = 1\n    def test_d(self): pass\n    class TestE:\n        def test_f(self): pass\n"
        },
    )
    durations = {
        "tests/test_heavy1.py::test_a[one]": 3.0,
        "tests/test_heavy1.py::test_a[two]": 4.0,
        "tests/test_heavy1.py::TestC::test_d": 5.0,
        "tests/test_heavy1.py::TestC::TestE::test_f": 6.0,
        "tests/test_other.py::test_a": 9.0,
    }
    assert ci_select.file_units(tmp_path, "tests/test_heavy1.py", durations) == {
        "tests/test_heavy1.py::test_a": 7.0,
        "tests/test_heavy1.py::test_b": 0.0,
        "tests/test_heavy1.py::TestC": 11.0,
    }


def test_a_file_holding_all_the_selected_time_stays_whole_on_one_leg(tmp_path):
    write(tmp_path, {"tests/test_heavy1.py": "def test_a(): pass\ndef test_b(): pass\n"})
    durations = {"tests/test_heavy1.py::test_a": 100.0, "tests/test_heavy1.py::test_b": 100.0}
    assert ci_select.legs_for(tmp_path, {"tests/test_heavy1.py"}, durations, 1) == [["tests/test_heavy1.py"]]


def test_an_unmeasured_file_weighs_nothing_on_its_leg(tmp_path):
    write(tmp_path, {f"tests/test_{name}.py": "def test_case(): pass\n" for name in ("a", "b", "x", "u")})
    durations = seconds(test_a=100.0, test_b=99.0, test_x=0.5)
    tests = {f"tests/test_{name}.py" for name in ("a", "b", "x", "u")}
    assert ci_select.legs_for(tmp_path, tests, durations, 4) == [
        ["tests/test_a.py"],
        ["tests/test_b.py", "tests/test_u.py", "tests/test_x.py"],
    ]


def test_the_split_units_of_one_file_share_its_collection_on_a_leg(tmp_path):
    padding = "#" + "x" * 2_000_000 + "\n"
    write(
        tmp_path,
        {
            "tests/test_heavy1.py": padding + "def test_a(): pass\ndef test_b(): pass\n",
            "tests/test_heavy2.py": padding + "def test_case(): pass\n",
            "tests/test_heavy3.py": padding + "def test_case(): pass\n",
        },
    )
    durations = {
        "tests/test_heavy1.py::test_a": 120.0,
        "tests/test_heavy1.py::test_b": 40.0,
        "tests/test_heavy2.py::TestCase::test_one": 30.0,
        "tests/test_heavy2.py::TestCase::test_two": 30.0,
        **seconds(test_heavy3=50.0),
    }
    tests = {"tests/test_heavy1.py", "tests/test_heavy2.py", "tests/test_heavy3.py"}
    assert ci_select.legs_for(tmp_path, tests, durations, 4) == [
        ["tests/test_heavy1.py::test_a", "tests/test_heavy1.py::test_b"],
        ["tests/test_heavy2.py", "tests/test_heavy3.py"],
    ]


def test_the_live_codex_file_runs_on_the_first_leg(repo):
    write(
        repo,
        {
            "tests/routing/test_codex_api.py": "from hooks import sample\n",
            "tests/test_late.py": "from hooks import sample\n",
        },
    )
    durations = seconds(test_sample=170.0, test_late=170.0)
    _, legs = decide(repo, ["hooks/sample.py"], durations)
    assert len(legs) == 2
    assert legs[0][0] == "tests/routing/test_codex_api.py"
    assert sum("tests/routing/test_codex_api.py" in leg for leg in legs) == 1


def test_the_changed_files_come_from_the_merge_base(repo):
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-c", "change")
    write(repo, {"hooks/sample.py": "VALUE = 3\n"})
    git(repo, "commit", "-am", "change")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "dev")
    write(repo, {"scripts/lonely.py": "VALUE = 4\n"})
    git(repo, "commit", "-am", "dev moved")
    assert ci_select.changed_files(repo, "dev", head) == ["hooks/sample.py"]
    assert ci_select.changed_files(repo, base, head) == ["hooks/sample.py"]


TREE_CHECK_FILES = {"tests/test_codeowners.py": "OWNERS = 1\n", "tests/test_ci_pull_request_api.py": "API = 1\n"}


@pytest.fixture
def tree(repo):
    write(repo, TREE_CHECK_FILES)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "tree checks")
    return repo


@pytest.mark.parametrize(
    "change",
    [
        ["mv", "scripts/lonely.py", "scripts/moved.py"],
        ["rm", "scripts/lonely.py"],
        ["add", "scripts/new.py"],
    ],
)
def test_adding_removing_or_moving_a_file_reshapes_the_tree(tree, change):
    if change[0] == "add":
        write(tree, {change[1]: "VALUE = 3\n"})
    git(tree, *change)
    git(tree, "commit", "-m", "reshape")
    assert ci_select.reshaped(tree, "HEAD~1", "HEAD") is True


def test_editing_a_file_leaves_the_tree_shape(tree):
    base = git(tree, "rev-parse", "HEAD")
    git(tree, "switch", "-c", "change")
    write(tree, {"hooks/sample.py": "VALUE = 3\n"})
    git(tree, "commit", "-am", "change")
    git(tree, "switch", "dev")
    git(tree, "mv", "scripts/lonely.py", "scripts/moved.py")
    git(tree, "commit", "-m", "dev moved")
    assert ci_select.reshaped(tree, "dev", "change") is False
    assert ci_select.reshaped(tree, base, "change") is False


def test_a_reshape_check_on_an_unknown_revision_fails(tree):
    with pytest.raises(subprocess.CalledProcessError) as error:
        ci_select.reshaped(tree, "not-a-revision", "HEAD")
    assert error.value.returncode == 128
    assert error.value.cmd == [
        "git",
        "diff",
        "--quiet",
        "--no-renames",
        "--diff-filter=AD",
        "not-a-revision...HEAD",
    ]


def test_a_reshaped_tree_selects_the_whole_tree_checks(tree):
    assert ci_select.decide(tree, ["hooks/sample.py"], {}, 3, ci_select.Change(reshaped=True)) == (
        "selected",
        [sorted(["tests/test_sample.py", *TREE_CHECK_FILES])],
    )


def test_a_reshaped_doc_selects_the_whole_tree_checks(tree):
    assert ci_select.decide(tree, ["docs/unnamed.md"], {}, 3, ci_select.Change(reshaped=True)) == (
        "selected",
        [sorted(TREE_CHECK_FILES)],
    )


def test_an_edited_file_leaves_the_whole_tree_checks_out(tree):
    assert decide(tree, ["hooks/sample.py"]) == ("selected", [["tests/test_sample.py"]])


def test_main_selects_the_whole_tree_checks_for_a_moved_file(tree, monkeypatch, tmp_path):
    git(tree, "mv", "scripts/lonely.py", "scripts/moved.py")
    git(tree, "commit", "-m", "move")
    assert run_main(tree, monkeypatch, tmp_path, "pull_request") == (
        "scope=selected\nlegs=[1]\ncount=1\nfiles_1=tests/test_ci_pull_request_api.py tests/test_codeowners.py\n"
    )


def test_the_workflow_keeps_a_one_file_change_selected_after_dev_moves(repo, monkeypatch, tmp_path):
    opened = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-c", "change")
    write(repo, {"hooks/sample.py": "VALUE = 3\n"})
    git(repo, "commit", "-am", "change")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "dev")
    for text in ("import os\n", "import sys\n"):
        write(repo, {"tests/conftest.py": text})
        git(repo, "add", ".")
        git(repo, "commit", "-m", "dev moved")
    git(repo, "update-ref", "refs/remotes/origin/dev", "dev")
    git(repo, "switch", "--detach", "dev")
    git(repo, "merge", "--no-ff", "-m", "pull merge", head)
    context = {
        "github.event.pull_request.base.sha": opened,
        "github.event.pull_request.head.sha": head,
        "github.base_ref": "dev",
        "github.event_name": "pull_request",
        "github.repository": "owner/repo",
        "steps.app-token.outputs.token || github.token": "token",
    }
    monkeypatch.setattr(ci_select, "find_record", lambda *_: None)
    step = next(s for s in yaml.safe_load(WORKFLOW.read_text())["jobs"]["select"]["steps"] if s.get("id") == "select")
    for name, value in step["env"].items():
        monkeypatch.setenv(name, EXPRESSION.sub(lambda found: context[found.group(1)], value))
    monkeypatch.setenv("GITHUB_WORKSPACE", str(repo))
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    line = next(line for line in step["run"].splitlines() if "scripts.ci_select" in line)
    words = shlex.split(line.partition("--durations")[0])
    argv = [os.path.expandvars(part) for part in words[words.index("scripts.ci_select") + 1 :]]
    assert ci_select.main(argv) == 0
    assert output.read_text() == "scope=selected\nlegs=[1]\ncount=1\nfiles_1=tests/test_sample.py\n"


def run_main(repo, monkeypatch, tmp_path, event, base="HEAD~1"):
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert ci_select.main(["--base", base, "--head", "HEAD", "--event", event, "--legs", "3", "--root", str(repo)]) == 0
    return output.read_text()


def test_main_writes_the_selected_legs_for_a_pull_request(repo, monkeypatch, tmp_path, capsys):
    write(repo, {"hooks/sample.py": "VALUE = 5\n", ".test_durations": json.dumps(seconds(test_sample=2.0))})
    git(repo, "commit", "-am", "change")
    written = run_main(repo, monkeypatch, tmp_path, "pull_request")
    expected = "scope=selected\nlegs=[1]\ncount=1\nfiles_1=tests/test_sample.py\n"
    assert written == expected
    assert capsys.readouterr().out == expected


def test_main_writes_every_selected_leg(heavy, monkeypatch, tmp_path):
    durations = seconds(test_sample=170.0, test_heavy1=170.0, test_heavy2=170.0, test_heavy3=1.0)
    write(heavy, {"hooks/sample.py": "VALUE = 6\n", ".test_durations": json.dumps(durations)})
    git(heavy, "add", ".")
    git(heavy, "commit", "-m", "change")
    written = run_main(heavy, monkeypatch, tmp_path, "pull_request").splitlines()
    assert written[:3] == ["scope=selected", "legs=[1, 2, 3]", "count=3"]
    assert [line.partition("=")[0] for line in written[3:]] == ["files_1", "files_2", "files_3"]


@pytest.mark.parametrize("event", ["merge_group", "push", "workflow_dispatch"])
def test_main_runs_the_full_suite_outside_pull_requests(repo, monkeypatch, tmp_path, event):
    write(repo, {"hooks/sample.py": "VALUE = 7\n"})
    git(repo, "commit", "-am", "change")
    assert run_main(repo, monkeypatch, tmp_path, event, base="not-a-revision") == "scope=full\nlegs=[]\ncount=0\n"


def test_main_reports_a_change_that_selects_nothing(repo, monkeypatch, tmp_path):
    write(repo, {"docs/unnamed.md": "x\n"})
    git(repo, "add", ".")
    git(repo, "commit", "-m", "docs")
    assert run_main(repo, monkeypatch, tmp_path, "pull_request") == "scope=none\nlegs=[]\ncount=0\n"


def test_main_prints_without_a_github_output(repo, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert (
        ci_select.main(["--base", "HEAD", "--head", "HEAD", "--event", "push", "--legs", "3", "--root", str(repo)]) == 0
    )
    assert capsys.readouterr().out == "scope=full\nlegs=[]\ncount=0\n"


@pytest.mark.parametrize("dropped", ["--base", "--head", "--event", "--legs"])
def test_main_requires_every_selection_input(repo, dropped):
    given = {"--base": "HEAD", "--head": "HEAD", "--event": "push", "--legs": "3"}
    del given[dropped]
    with pytest.raises(SystemExit) as refused:
        ci_select.main([part for pair in given.items() for part in pair] + ["--root", str(repo)])
    assert refused.value.code == 2


def test_main_reads_the_working_folder_by_default(heavy, monkeypatch, tmp_path):
    write(heavy, {"hooks/sample.py": "VALUE = 9\n", ".test_durations": json.dumps(seconds(test_sample=1.0))})
    git(heavy, "commit", "-am", "change")
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.chdir(heavy)
    assert ci_select.main(["--base", "HEAD~1", "--head", "HEAD", "--event", "pull_request", "--legs", "3"]) == 0
    assert output.read_text() == (
        "scope=selected\nlegs=[1]\ncount=1\n"
        "files_1=tests/test_heavy1.py tests/test_heavy2.py tests/test_heavy3.py tests/test_sample.py\n"
    )


def test_main_budgets_on_the_given_durations_with_later_files_winning(heavy, monkeypatch, tmp_path):
    write(heavy, {"hooks/sample.py": "VALUE = 8\n"})
    git(heavy, "commit", "-am", "change")
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    first.write_text(json.dumps(seconds(test_sample=600.0, test_heavy1=170.0)))
    second.write_text(json.dumps(seconds(test_sample=1.0)))
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    arguments = ["--base", "HEAD~1", "--head", "HEAD", "--event", "pull_request", "--legs", "3", "--root", str(heavy)]
    assert ci_select.main([*arguments, "--durations", str(first), str(second)]) == 0
    assert output.read_text().splitlines()[:3] == ["scope=selected", "legs=[1, 2]", "count=2"]


def jobs():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def workflow_legs():
    step = next(s for s in jobs()["select"]["steps"] if s.get("id") == "select")
    words = shlex.split(next(line for line in step["run"].splitlines() if "scripts.ci_select" in line))
    return int(words[words.index("--legs") + 1])


def test_the_workflow_selects_nine_legs_for_a_large_change(repo):
    write(repo, {f"tests/test_tick{index}.py": "from hooks import sample\n" for index in range(1, 10)})
    durations = seconds(test_sample=15.0, **{f"test_tick{index}": 160.0 for index in range(1, 10)})
    scope, legs = decide(repo, ["hooks/sample.py"], durations, workflow_legs())
    assert scope == "selected"
    assert len(legs) == 9


def test_the_workflow_caps_a_selection_at_the_unit_shard_count(repo):
    write(repo, {f"tests/test_tick{index}.py": "from hooks import sample\n" for index in range(1, 31)})
    durations = seconds(test_sample=15.0, **{f"test_tick{index}": ci_select.LEG_SECONDS for index in range(1, 31)})
    scope, legs = decide(repo, ["hooks/sample.py"], durations, workflow_legs())
    assert scope == "selected"
    assert len(legs) == workflow_legs() == len(jobs()["unit"]["strategy"]["matrix"]["shard"])
    assert sorted(file for leg in legs for file in leg) == sorted(
        ["tests/test_sample.py", *(f"tests/test_tick{index}.py" for index in range(1, 31))]
    )


def test_a_selected_leg_has_the_time_of_a_full_suite_shard():
    assert step_seconds("unit-selected") >= step_seconds("unit")
    assert jobs()["unit-selected"]["timeout-minutes"] >= jobs()["unit"]["timeout-minutes"]


def test_the_workflow_hands_every_leg_its_files(repo, monkeypatch, tmp_path, capsys):
    write(repo, {f"tests/test_tick{index}.py": "from hooks import sample\n" for index in range(1, 10)})
    durations = seconds(test_sample=15.0, **{f"test_tick{index}": 160.0 for index in range(1, 10)})
    write(repo, {"hooks/sample.py": "VALUE = 4\n", ".test_durations": json.dumps(durations)})
    git(repo, "add", ".")
    git(repo, "commit", "-m", "change")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    arguments = ["--base", "HEAD~1", "--head", "HEAD", "--event", "pull_request", "--legs", str(workflow_legs())]
    assert ci_select.main([*arguments, "--root", str(repo)]) == 0
    printed = capsys.readouterr().out
    (tmp_path / "selection").mkdir()
    (tmp_path / "selection/selection.txt").write_text(printed)
    upload = next(s for s in jobs()["select"]["steps"] if s.get("name") == "Upload the selected legs")
    assert upload["with"]["path"] == "${{ runner.temp }}/selection/selection.txt"
    assert (
        'tee "$RUNNER_TEMP/selection/selection.txt"'
        in next(s for s in jobs()["select"]["steps"] if s.get("id") == "select")["run"]
    )
    read = next(s for s in jobs()["unit-selected"]["steps"] if s.get("name") == "Read this leg's test paths")
    expected = dict(line.split("=", 1) for line in printed.splitlines() if line.startswith("files_"))
    assert len(expected) == 9
    for leg in range(1, 10):
        environment = tmp_path / f"env-{leg}"
        given = {"PATH": os.environ["PATH"], "RUNNER_TEMP": str(tmp_path), "LEG": str(leg)}
        subprocess.run(["bash", "-e", "-c", read["run"]], check=True, env=given | {"GITHUB_ENV": str(environment)})
        assert environment.read_text() == f"FILES={expected[f'files_{leg}']}\n"


def test_a_recorded_read_selects_the_tests_that_read_the_changed_file_as_data(repo):
    write(
        repo,
        {
            ".github/workflows/deploy.yml": "name: deploy\n",
            "hooks/scanned.py": "VALUE = 3\n",
            "tests/test_workflows.py": "import os\n",
            "tests/test_boundary.py": "import os\n",
        },
    )
    manifest = {
        "units": ["hooks/sample.py::<module>"],
        "tests": {
            "tests/test_workflows.py::test_scan": {
                "units": [],
                "files": [".test_durations", ".github/workflows/deploy.yml"],
            },
            "tests/test_boundary.py::test_boundary": {"units": [], "files": [".test_durations", "hooks/scanned.py"]},
            "tests/test_other.py::test_other": {"units": [], "files": [".test_durations"]},
        },
    }
    for changed, expected in (
        ([".github/workflows/deploy.yml"], {"tests/test_workflows.py"}),
        (["hooks/scanned.py"], {"tests/test_boundary.py"}),
        ([".test_durations"], set()),
    ):
        assert ci_select.selected_tests(repo, changed, False, manifest) == expected


def test_a_recorded_method_unit_selects_its_test_by_the_file_before_the_first_separator():
    manifest = {
        "units": ["hooks/sample.py::Box::size"],
        "tests": {"tests/test_box.py::test_size": {"units": [0]}, "tests/test_other.py::test_other": {"units": []}},
    }
    assert ci_select.recorded_tests(manifest, ["hooks/sample.py"]) == {"tests/test_box.py"}


@pytest.mark.feature("selectors-read-outside-files")
def test_a_change_to_a_file_a_test_opened_outside_its_record_selects_that_test_file() -> None:
    manifest = {
        "units": [],
        "tests": {
            "tests/test_a.py::test_one": {"units": [], "files": [], "outside": ["docs/a.md"]},
            "tests/test_b.py::test_two": {"units": [], "files": ["docs/b.md"]},
            "tests/test_c.py::test_three": {"units": []},
        },
    }
    assert ci_select.recorded_tests(manifest, ["docs/a.md"]) == {"tests/test_a.py"}
    assert ci_select.recorded_tests(manifest, ["docs/b.md"]) == {"tests/test_b.py"}
    assert ci_select.recorded_tests(manifest, ["docs/other.md"]) == set()


def test_main_reads_the_recorded_reads_for_a_change_without_python_source(repo, monkeypatch, tmp_path):
    write(repo, {"tests/test_workflows.py": "import os\n", "deploy/values.yaml": "a: 1\n"})
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    write(repo, {"deploy/values.yaml": "a: 2\n"})
    git(repo, "commit", "-am", "change")
    monkeypatch.setenv("CI_SELECT_REPOSITORY", "owner/repo")
    manifest = {
        "units": [],
        "tests": {
            "tests/test_workflows.py::test_scan": {"units": [], "files": ["deploy/values.yaml"]},
            "tests/test_other.py::test_other": {"units": [], "files": []},
        },
    }
    monkeypatch.setattr(ci_select, "find_record", lambda *_: ({"id": 7}, manifest))
    output = run_main(repo, monkeypatch, tmp_path, "pull_request")
    assert "scope=selected" in output
    assert "files_1=tests/test_workflows.py" in output


@pytest.mark.parametrize("name", ["snapshot.js", "new_contract.js"])
def test_selection_takes_every_python_wrapper_of_a_changed_javascript_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    source = f"scripts/static/{name}"
    files = {
        source: "export const value = 2;\n",
        "tests/test_direct.py": f"PAGE = '{source}'\n",
        "tests/nested/test_basename.py": f"PAGE = '{name}'\n",
        "tests/js_loader.py": f"PAGE = '{name}'\n",
        "tests/second_loader.py": "from tests import js_loader\n",
        "tests/test_indirect.py": "from tests import second_loader\n",
        "tests/test_unrelated.py": "def test_other(): pass\n",
    }
    for path, text in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    monkeypatch.chdir(tmp_path)
    assert ci_select.selected_tests(tmp_path, [source], False) == {
        "tests/test_direct.py",
        "tests/nested/test_basename.py",
        "tests/test_indirect.py",
    }
