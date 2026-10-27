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

from scripts import ci_select
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
        ["tests/flake_ledger.py"],
        ["tests/browser_coverage.py"],
        ["tests/node_capture.cjs"],
        ["tests/conftest.py"],
        ["tests/swarm/conftest.py", "tests/test_sample.py"],
    ],
)
def test_a_change_to_the_test_harness_runs_the_full_suite(repo, changed):
    assert decide(repo, changed) == ("full", [])


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


@pytest.mark.parametrize("path", ["scripts/config.json", "docs/notes.md", "other.py"])
def test_main_does_not_fetch_coverage_for_changes_outside_python_sources(repo, monkeypatch, tmp_path, path):
    write(repo, {path: "{}"})
    git(repo, "add", path)
    git(repo, "commit", "-m", "change")
    monkeypatch.setenv("CI_SELECT_REPOSITORY", "owner/repo")

    def record(*_):
        pytest.fail("Coverage lookup is unnecessary for a change outside Python sources")

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
    assert ci_select.decide(tree, ["hooks/sample.py"], {}, 3, tree_reshaped=True) == (
        "selected",
        [sorted(["tests/test_sample.py", *TREE_CHECK_FILES])],
    )


def test_a_reshaped_doc_selects_the_whole_tree_checks(tree):
    assert ci_select.decide(tree, ["docs/unnamed.md"], {}, 3, tree_reshaped=True) == (
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
    argv = [os.path.expandvars(part) for part in shlex.split(line.partition("--durations")[0])[3:]]
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
