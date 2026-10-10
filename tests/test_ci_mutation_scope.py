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
    assert bases == sorted([pinned, earlier, merged_dev])
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
    assert bases == sorted([base, first] if green else [base])
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


@pytest.mark.parametrize(("edit", "counted"), [("    assert f() == 1\n    assert f()\n", True), ("    f()\n", False)])
def test_an_earlier_branch_counts_only_while_the_head_keeps_every_test_line_it_was_graded_with(tmp_path, edit, counted):
    from scripts.ci_mutation.scope import own_bases

    git, commit = _repo(tmp_path)
    base = commit("dev.py", "a = 1\n")
    git("checkout", "-q", "-b", "branch")
    (tmp_path / "tests").mkdir()
    test = tmp_path / "tests" / "test_one.py"
    test.write_text("def test_f():\n    assert f() == 1\n")
    git("add", "tests/test_one.py")
    earlier = commit("one.py", "def f():\n    return 1\n")
    git("update-ref", "refs/remotes/origin/earlier", earlier)
    test.write_text("def test_f():\n" + edit)
    (tmp_path / "tests" / "test_two.py").write_text("def test_g():\n    assert g() == 2\n")
    git("add", "tests/test_one.py", "tests/test_two.py")
    commit("two.py", "def g():\n    return 2\n")
    asked = []

    def graded(sha):
        asked.append(sha)
        return True

    assert own_bases(tmp_path, base, "HEAD", graded) == sorted([base, earlier] if counted else [base])
    assert asked == ([earlier] if counted else [])


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        ({"mutation": (0, "11\n")}, True),
        ({"mutation": (0, ""), "Gate — Required": (0, "5\n6\n"), "mutation (0)": (0, "6\n")}, True),
        ({"mutation": (0, ""), "Gate — Required": (0, "5\n"), "mutation (0)": (0, "7\n")}, False),
        ({"mutation": (1, "9\n"), "Gate — Required": (0, "5\n"), "mutation (0)": (1, "5\n")}, False),
    ],
)
def test_a_commit_is_graded_only_by_a_green_preflight_or_a_green_gate_whose_run_mutated(monkeypatch, answers, expected):
    import subprocess

    from scripts.ci_mutation import scope

    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    calls = []

    def run(command, **kwargs):
        check = command[6].removeprefix("check_name=")
        calls.append((command, kwargs))
        code, out = answers[check]
        return subprocess.CompletedProcess(command, code, out, "")

    monkeypatch.setattr(scope.subprocess, "run", run)
    assert scope.graded_green("abc") is expected
    assert calls == [
        (
            [
                "gh",
                "api",
                "-X",
                "GET",
                "repos/owner/repo/commits/abc/check-runs",
                "-f",
                f"check_name={check}",
                "-f",
                "status=completed",
                "--jq",
                '.check_runs[] | select(.conclusion == "success") | .check_suite.id',
            ],
            {"capture_output": True, "text": True},
        )
        for check in answers
    ]
