from pathlib import Path

import pytest

from scripts.ci_mutation.scope import changed_lines, select_tests


def test_hunks_include_only_added_and_changed_head_lines():
    diff = "@@ -3,2 +3,3 @@\n-old\n+new\n+extra\n@@ -12 +13 @@\n-old\n+new\n@@ -20,2 +21,0 @@\n-deleted\n"
    assert changed_lines(diff) == {3, 4, 5, 13}


def test_tests_are_discovered_by_module_name_and_import(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    nested = tests / "nested"
    nested.mkdir()
    (nested / "test_sample.py").write_text("pass\n")
    (tests / "test_import.py").write_text("from hooks.context import sample\n")
    (tests / "test_direct.py").write_text("import hooks.context.sample as sample\n")
    (tests / "test_other.py").write_text("from hooks.context import other\n")
    assert select_tests(tmp_path, Path("hooks/context/sample.py")) == [
        "tests/nested/test_sample.py",
        "tests/test_direct.py",
        "tests/test_import.py",
    ]


def test_package_initializer_selects_ordinary_package_imports(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_direct.py").write_text("import hooks.context.sample as sample\n")
    (tests / "test_from_parent.py").write_text("from hooks.context import sample\n")
    (tests / "test_from_package.py").write_text("from hooks.context.sample import value\n")
    (tests / "test_other.py").write_text("from hooks.context import other\n")
    assert select_tests(tmp_path, Path("hooks/context/sample/__init__.py")) == [
        "tests/test_direct.py",
        "tests/test_from_package.py",
        "tests/test_from_parent.py",
    ]


def test_tests_reaching_the_module_through_test_helpers_are_selected(tmp_path):
    swarm = tmp_path / "tests" / "swarm"
    swarm.mkdir(parents=True)
    (swarm / "__init__.py").write_text("")
    (swarm / "test_cli.py").write_text("from scripts.swarm import cli\n\n\ndef run():\n    return cli\n")
    (swarm / "cases.py").write_text("from .test_cli import run\n")
    (swarm / "test_kinds.py").write_text("from tests.swarm.test_cli import run\n")
    (swarm / "test_chain.py").write_text("from tests.swarm import cases\n")
    (swarm / "test_relative.py").write_text("from . import cases\n")
    (swarm / "test_other.py").write_text("from scripts.swarm import prompt\n")
    assert select_tests(tmp_path, Path("scripts/swarm/cli.py")) == [
        "tests/swarm/test_chain.py",
        "tests/swarm/test_cli.py",
        "tests/swarm/test_kinds.py",
        "tests/swarm/test_relative.py",
    ]


def test_a_change_reached_only_through_a_helper_selects_the_indirect_test(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "commands_cases.py").write_text("import hooks.context.commands as commands\nimport tests.loop_cases\n")
    (tests / "loop_cases.py").write_text("from tests import commands_cases\n")
    (tests / "test_indirect.py").write_text("import tests.loop_cases\n")
    (tests / "test_unrelated.py").write_text("import tests.other_cases\n")
    (tests / "other_cases.py").write_text("from hooks.context import other\n")
    assert select_tests(tmp_path, Path("hooks/context/commands.py")) == ["tests/test_indirect.py"]


def test_parent_relative_imports_and_prefixed_names_resolve_exactly(tmp_path):
    nested = tmp_path / "tests" / "a" / "b"
    nested.mkdir(parents=True)
    (tmp_path / "tests" / "a" / "c.py").write_text("from scripts.swarm import cli\n")
    (nested / "test_parent.py").write_text("from ..c import run\n")
    (nested / "test_client.py").write_text("import scripts.swarm.client\n")
    assert select_tests(tmp_path, Path("scripts/swarm/cli.py")) == ["tests/a/b/test_parent.py"]


def test_a_helper_patching_the_module_by_name_selects_its_importers(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "patches.py").write_text('from unittest import mock\n\nquiet = mock.patch("scripts.swarm.cli.run")\n')
    (tests / "test_patched.py").write_text("from tests.patches import quiet\n")
    assert select_tests(tmp_path, Path("scripts/swarm/cli.py")) == ["tests/test_patched.py"]


def test_a_rewritten_test_module_is_selected_again(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_late.py").write_text("from hooks.context import aaaaaa\n")
    assert select_tests(tmp_path, Path("hooks/context/sample.py")) == []
    (tests / "test_late.py").write_text("from hooks.context import sample\n")
    assert select_tests(tmp_path, Path("hooks/context/sample.py")) == ["tests/test_late.py"]


def test_a_folder_named_like_a_module_is_skipped(tmp_path):
    tests = tmp_path / "tests"
    (tests / "a.py").mkdir(parents=True)
    (tests / "test_b.py").write_text("from hooks.context import sample\n")
    assert select_tests(tmp_path, Path("hooks/context/sample.py")) == ["tests/test_b.py"]


def test_diff_discovers_only_changed_source_python_files(tmp_path):
    import subprocess

    from scripts.ci_mutation.scope import discover_changes

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path).decode().strip()

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    (tmp_path / "hooks").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "hooks" / "gone.py").write_text("removed = 1\n")
    (tmp_path / "hooks" / "same.py").write_text("untouched = 1\n")
    git("add", "hooks/gone.py", "hooks/same.py")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "hooks" / "gone.py").unlink()
    (tmp_path / "hooks" / "new.py").write_text("def f():\n    return 3\n")
    (tmp_path / "scripts" / "space name.py").write_text("def f():\n    return 2\n")
    (tmp_path / "tests" / "test_other.py").write_text("pass\n")
    (tmp_path / "scripts" / "note.txt").write_text("prose\n")
    git("add", "hooks/gone.py", "hooks/new.py", "scripts/space name.py", "tests/test_other.py", "scripts/note.txt")
    git("commit", "-m", "head")
    assert discover_changes(tmp_path, base, "HEAD") == {"hooks/new.py": {1, 2}, "scripts/space name.py": {1, 2}}


def _repo(tmp_path):
    import subprocess

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path).decode().strip()

    def commit(name, text):
        path = tmp_path / "hooks" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
        git("add", f"hooks/{name}")
        git("commit", "-q", "-m", name)
        return git("rev-parse", "HEAD")

    git("init", "-q", "-b", "dev")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    return git, commit


def test_a_branch_refreshed_with_dev_grades_only_its_own_lines_against_a_pinned_older_base(tmp_path):
    from scripts.ci_mutation.scope import discover_changes

    git, commit = _repo(tmp_path)
    pinned = commit("dev.py", "a = 1\n")
    git("checkout", "-q", "-b", "branch")
    commit("own.py", "def f():\n    return 1\n")
    git("checkout", "-q", "dev")
    commit("dev.py", "a = 1\nb = 2\n")
    git("update-ref", "refs/remotes/origin/dev", "HEAD")
    git("checkout", "-q", "branch")
    git("merge", "-q", "--no-edit", "dev")
    assert discover_changes(tmp_path, pinned, "HEAD") == {"hooks/own.py": {1, 2}}


def test_a_stacked_branch_grades_only_its_own_lines_once_the_earlier_branch_was_graded_green(tmp_path):
    from scripts.ci_mutation.scope import discover_changes, own_bases

    git, commit = _repo(tmp_path)
    pinned = commit("dev.py", "a = 1\n")
    git("checkout", "-q", "-b", "earlier")
    earlier = commit("earlier.py", "def e():\n    return 1\n")
    git("update-ref", "refs/remotes/origin/earlier", "HEAD")
    git("checkout", "-q", "-b", "stacked")
    commit("own.py", "def f():\n    return 1\n")
    commit("earlier.py", "def e():\n    return 2\n")
    git("checkout", "-q", "dev")
    commit("dev.py", "a = 1\nb = 2\n")
    git("update-ref", "refs/remotes/origin/dev", "HEAD")
    merged_dev = git("rev-parse", "HEAD")
    git("checkout", "-q", "stacked")
    git("merge", "-q", "--no-edit", "dev")
    bases = own_bases(tmp_path, pinned, "HEAD", [earlier].__contains__)
    assert bases == [pinned, *sorted([earlier, merged_dev])]
    assert discover_changes(tmp_path, bases, "HEAD") == {"hooks/own.py": {1, 2}, "hooks/earlier.py": {2}}
    assert discover_changes(tmp_path, pinned, "HEAD") == {"hooks/own.py": {1, 2}, "hooks/earlier.py": {1, 2}}


@pytest.mark.parametrize("green", [False, True])
def test_only_a_pushed_commit_inside_the_head_and_graded_green_counts_as_an_earlier_base(tmp_path, green):
    from scripts.ci_mutation.scope import discover_changes, own_bases

    git, commit = _repo(tmp_path)
    base = commit("dev.py", "a = 1\n")
    git("update-ref", "refs/remotes/origin/dev", "HEAD")
    git("checkout", "-q", "-b", "branch")
    first = commit("one.py", "def f():\n    return 1\n")
    git("update-ref", "refs/remotes/origin/sibling", first)
    git("checkout", "-q", "-b", "fork")
    git("update-ref", "refs/remotes/origin/fork", commit("fork.py", "def h():\n    return 3\n"))
    git("checkout", "-q", "branch")
    commit("mid.py", "def m():\n    return 4\n")
    git("branch", "local")
    commit("two.py", "def g():\n    return 2\n")
    git("update-ref", "refs/remotes/origin/branch", "HEAD")
    asked = []

    def graded(sha):
        asked.append(sha)
        return green

    bases = own_bases(tmp_path, base, "HEAD", graded)
    assert asked == [first]
    assert bases == ([base, first] if green else [base])
    own = {"hooks/mid.py": {1, 2}, "hooks/two.py": {1, 2}} | ({} if green else {"hooks/one.py": {1, 2}})
    assert discover_changes(tmp_path, bases, "HEAD") == own


def test_a_line_left_as_the_given_base_had_it_is_never_graded(tmp_path):
    from scripts.ci_mutation.scope import discover_changes

    git, commit = _repo(tmp_path)
    pinned = commit("kept.py", "def k():\n    return 1\n")
    git("checkout", "-q", "-b", "branch")
    commit("own.py", "def f():\n    return 1\n")
    git("checkout", "-q", "dev")
    commit("kept.py", "def k():\n    return 2\n")
    git("update-ref", "refs/remotes/origin/dev", "HEAD")
    git("checkout", "-q", "branch")
    git("merge", "-q", "--no-edit", "dev")
    commit("kept.py", "def k():\n    return 1\n")
    assert discover_changes(tmp_path, pinned, "HEAD") == {"hooks/own.py": {1, 2}}


def test_resolved_bases_are_graded_without_looking_up_branches_again(tmp_path):
    from scripts.ci_mutation.scope import discover_changes

    git, commit = _repo(tmp_path)
    commit("dev.py", "a = 1\n")
    git("checkout", "-q", "-b", "branch")
    earlier = commit("one.py", "def f():\n    return 1\n")
    commit("two.py", "def g():\n    return 2\n")
    assert discover_changes(tmp_path, [earlier], "HEAD") == {"hooks/two.py": {1, 2}}


PASSING = "import pytest\n\n\ndef test_a():\n    assert 1\n\n\ndef test_b():\n    assert 2\n"
FIXTURED = PASSING + "\n\n@pytest.fixture\ndef thing():\n    return 1\n"


@pytest.mark.parametrize(
    ("name", "old", "new", "expected"),
    [
        ("tests/test_x.py", PASSING, PASSING + "\n\ndef test_c():\n    assert 3\n", True),
        ("tests/test_x.py", FIXTURED, FIXTURED + "\n\ndef test_c():\n    assert 3\n", True),
        ("tests/test_x.py", PASSING, PASSING + "\n\nasync def test_c():\n    assert 3\n", True),
        ("tests/test_x.py", PASSING, PASSING + "\n\nclass TestC:\n    def test_c(self):\n        assert 3\n", True),
        ("tests/test_x.py", PASSING, "import os\n" + PASSING, True),
        ("tests/test_x.py", PASSING, "from os import path\n" + PASSING, True),
        ("tests/test_x.py", PASSING, PASSING.replace("def test_a", "@pytest.mark.skip\ndef test_a"), False),
        ("tests/test_x.py", PASSING, PASSING.replace("assert 2", "assert 2 or True"), False),
        ("tests/test_x.py", PASSING, PASSING + "\n\ndef helper():\n    return 3\n", False),
        ("tests/test_x.py", PASSING, PASSING + "\n\nclass Helper:\n    pass\n", False),
        ("tests/test_x.py", PASSING, PASSING + "\nx = 1\n", False),
        ("tests/test_x.py", PASSING, PASSING + "\n\ndef test_a():\n    pass\n", False),
        ("tests/test_x.py", PASSING, PASSING + "\nfrom os import path as pytest\n", False),
        ("tests/test_x.py", "import os\n" + PASSING, "import os\nimport os.path\n" + PASSING, False),
        ("tests/test_x.py", PASSING, PASSING + "\n\ndef test_c():\n    pass\n\n\ndef test_c():\n    pass\n", False),
        (
            "tests/test_x.py",
            PASSING,
            PASSING + "\n\n@pytest.fixture(autouse=True)\ndef test_patch():\n    pass\n",
            False,
        ),
        ("tests/test_x.py", PASSING, PASSING + "\n\ndef testing():\n    pass\n", False),
        (
            "tests/test_x.py",
            PASSING,
            PASSING + "\n\n@pytest.mark.parametrize('x', [1])\ndef test_c(x):\n    assert x\n",
            True,
        ),
        ("tests/test_x.py", PASSING, PASSING + "\n\n@pytest.mark.slow\ndef test_c():\n    assert 3\n", True),
        (
            "tests/test_x.py",
            PASSING,
            PASSING
            + "\n\n@pytest.mark.slow\nclass TestC:\n    @pytest.mark.slow\n    def test_c(self):\n        assert 3\n",
            True,
        ),
        (
            "tests/test_x.py",
            PASSING,
            PASSING + "\n\nclass TestC:\n    patched = 1\n\n    def test_c(self):\n        assert 3\n",
            False,
        ),
        ("tests/test_x.py", PASSING, PASSING + "\n\nclass TestC:\n    def helper(self):\n        return 3\n", False),
        (
            "tests/test_x.py",
            PASSING,
            PASSING + "\n\nclass TestC:\n    import os\n\n    def test_c(self):\n        assert 3\n",
            False,
        ),
        (
            "tests/test_x.py",
            PASSING,
            PASSING + "\n\n@stub\nclass TestC:\n    def test_c(self):\n        assert 3\n",
            False,
        ),
        ("tests/test_x.py", PASSING, PASSING.replace("def test_b():\n    assert 2\n", ""), False),
        ("tests/test_x.py", None, PASSING, True),
        ("tests/test_x.py", PASSING, None, False),
        ("tests/cases.py", PASSING, PASSING + "\n\ndef test_c():\n    assert 3\n", False),
        ("tests/test_x.json", "{}\n", '{"a": 1}\n', False),
    ],
)
def test_a_test_file_change_counts_as_harmless_only_when_it_adds_tests(tmp_path, name, old, new, expected):
    from scripts.ci_mutation.scope import adds_only_tests

    git, commit = _repo(tmp_path)
    path = tmp_path / name
    path.parent.mkdir(exist_ok=True)
    if old is not None:
        path.write_text(old)
        git("add", name)
    base = commit("dev.py", "a = 1\n")
    if new is None:
        git("rm", "-q", name)
    else:
        path.write_text(new)
        git("add", name)
    commit("dev.py", "a = 2\n")
    assert adds_only_tests(tmp_path, base, "HEAD", name) is expected


def test_weakened_tests_name_every_test_change_beyond_added_tests(tmp_path):
    from scripts.ci_mutation.scope import weakened_tests

    git, commit = _repo(tmp_path)
    (tmp_path / "tests").mkdir()
    for name in ("test_add.py", "test_skip.py", "test_moved.py"):
        (tmp_path / "tests" / name).write_text(PASSING)
    git("add", "tests")
    base = commit("dev.py", "a = 1\n")
    (tmp_path / "tests" / "test_add.py").write_text(PASSING + "\n\ndef test_c():\n    assert 3\n")
    (tmp_path / "tests" / "test_skip.py").write_text(PASSING.replace("def test_a", "@pytest.mark.skip\ndef test_a"))
    git("mv", "tests/test_moved.py", "tests/test_renamed.py")
    (tmp_path / "tests" / "test_renamed.py").write_text(PASSING.replace("assert 2", "assert 2 or True"))
    git("add", "tests")
    commit("dev.py", "a = 2\n")
    assert weakened_tests(tmp_path, base, "HEAD") == {"tests/test_skip.py", "tests/test_moved.py"}


@pytest.mark.parametrize(
    ("edit", "regraded"),
    [
        (lambda text: text + "\n\ndef test_more():\n    assert dev.d() == 1\n", False),
        (lambda text: text.replace("def test_d", "@pytest.mark.skip\ndef test_d"), True),
        (lambda text: text.replace("from hooks import dev", "from tests import fakes as dev"), True),
        (None, True),
    ],
)
def test_inherited_lines_are_regraded_once_the_branch_weakens_the_tests_that_select_them(tmp_path, edit, regraded):
    from scripts.ci_mutation.scope import discover_changes

    git, commit = _repo(tmp_path)
    (tmp_path / "tests").mkdir()
    test = tmp_path / "tests" / "test_dev.py"
    test.write_text("import pytest\n\nfrom hooks import dev\n\n\ndef test_d():\n    assert dev.d() == 1\n")
    git("add", "tests")
    pinned = commit("dev.py", "def d():\n    return 1\n")
    git("checkout", "-q", "-b", "branch")
    if edit is None:
        (tmp_path / "tests" / "helpers.py").write_text("STUB = True\n")
    else:
        test.write_text(edit(test.read_text()))
    git("add", "tests")
    commit("own.py", "def f():\n    return 1\n")
    git("checkout", "-q", "dev")
    commit("dev.py", "def d():\n    return 1\n\n\ndef e():\n    return 2\n")
    git("update-ref", "refs/remotes/origin/dev", "HEAD")
    git("checkout", "-q", "branch")
    git("merge", "-q", "--no-edit", "dev")
    own = {"hooks/own.py": {1, 2}}
    assert discover_changes(tmp_path, pinned, "HEAD") == (own | {"hooks/dev.py": {3, 4, 5, 6}} if regraded else own)


@pytest.mark.parametrize(
    ("runs", "jobs", "asked", "expected"),
    [
        ((0, "7\n"), {"7": (0, "1\n")}, ["7"], True),
        ((0, "7\n8\n"), {"7": (0, "0\n"), "8": (0, "1\n")}, ["7", "8"], True),
        ((0, "7\n8\n"), {"7": (0, "2\n"), "8": (0, "0\n")}, ["7"], True),
        ((0, "7\n"), {"7": (0, "0\n")}, ["7"], False),
        ((0, "7\n"), {"7": (0, "")}, ["7"], False),
        ((0, "7\n"), {"7": (1, "1\n")}, ["7"], False),
        ((1, "7\n"), {"7": (0, "1\n")}, [], False),
        ((0, ""), {}, [], False),
    ],
)
def test_a_commit_is_graded_only_by_a_push_preflight_whose_mutation_job_passed(
    monkeypatch, runs, jobs, asked, expected
):
    import subprocess

    from scripts.ci_mutation import scope

    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        code, out = jobs[command[4].split("/")[4]] if len(calls) > 1 else runs
        return subprocess.CompletedProcess(command, code, out, "")

    monkeypatch.setattr(scope.subprocess, "run", run)
    assert scope.graded_green("abc") is expected
    api = ["gh", "api", "-X", "GET"]
    green = '[.jobs[] | select(.name == "mutation" and .conclusion == "success")] | length'
    listed = [
        *api,
        "repos/owner/repo/actions/workflows/mutation-preflight.yml/runs",
        "-f",
        "head_sha=abc",
        "-f",
        "event=push",
        "--jq",
        ".workflow_runs[].id",
    ]
    expected_calls = [listed] + [[*api, f"repos/owner/repo/actions/runs/{run}/jobs", "--jq", green] for run in asked]
    assert calls == [(command, {"capture_output": True, "text": True}) for command in expected_calls]
