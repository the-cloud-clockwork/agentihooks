from pathlib import Path

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


def _repo(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)

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


def test_a_branch_refreshed_with_dev_grades_only_its_own_lines_against_a_pinned_older_base(tmp_path, monkeypatch):
    from scripts.ci_mutation.scope import discover_changes

    git, commit = _repo(tmp_path, monkeypatch)
    pinned = commit("dev.py", "a = 1\n")
    git("checkout", "-q", "-b", "branch")
    commit("own.py", "def f():\n    return 1\n")
    git("checkout", "-q", "dev")
    commit("dev.py", "a = 1\nb = 2\n")
    git("update-ref", "refs/remotes/origin/dev", "HEAD")
    git("checkout", "-q", "branch")
    git("merge", "-q", "--no-edit", "dev")
    assert discover_changes(tmp_path, pinned, "HEAD") == {"hooks/own.py": {1, 2}}


def test_a_stacked_branch_grades_only_its_own_lines_after_a_dev_refresh(tmp_path, monkeypatch):
    from scripts.ci_mutation.scope import discover_changes, own_bases

    git, commit = _repo(tmp_path, monkeypatch)
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
    assert own_bases(tmp_path, pinned, "HEAD") == sorted([earlier, merged_dev])
    assert discover_changes(tmp_path, pinned, "HEAD") == {"hooks/own.py": {1, 2}, "hooks/earlier.py": {2}}


def test_copies_of_the_branch_itself_never_count_as_an_earlier_branch(tmp_path, monkeypatch):
    from scripts.ci_mutation.scope import discover_changes

    git, commit = _repo(tmp_path, monkeypatch)
    base = commit("dev.py", "a = 1\n")
    git("update-ref", "refs/remotes/origin/dev", "HEAD")
    git("checkout", "-q", "-b", "branch")
    first = commit("one.py", "def f():\n    return 1\n")
    for ref in ("branch", "diffcheck/branch-red", "wip/branch", "gh-readonly-queue/dev/pr-1", "main"):
        git("update-ref", f"refs/remotes/origin/{ref}", first)
    commit("two.py", "def g():\n    return 2\n")
    monkeypatch.setenv("GITHUB_REF_NAME", "branch")
    assert discover_changes(tmp_path, base, "HEAD") == {"hooks/one.py": {1, 2}, "hooks/two.py": {1, 2}}


def test_resolved_bases_are_graded_without_looking_up_branches_again(tmp_path, monkeypatch):
    from scripts.ci_mutation.scope import discover_changes

    git, commit = _repo(tmp_path, monkeypatch)
    base = commit("dev.py", "a = 1\n")
    git("checkout", "-q", "-b", "branch")
    earlier = commit("one.py", "def f():\n    return 1\n")
    commit("two.py", "def g():\n    return 2\n")
    git("update-ref", "refs/remotes/origin/earlier", earlier)
    assert discover_changes(tmp_path, [base], "HEAD") == {"hooks/one.py": {1, 2}, "hooks/two.py": {1, 2}}
