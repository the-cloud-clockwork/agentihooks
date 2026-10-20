import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import ci_select

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


def test_a_selection_over_its_leg_budget_runs_the_full_suite(heavy):
    budget = ci_select.LEG_SECONDS
    durations = seconds(test_sample=1.0, test_heavy1=budget, test_heavy2=budget, test_heavy3=budget - 0.5)
    assert decide(heavy, ["hooks/sample.py"], durations) == ("full", [])


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
    }
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
