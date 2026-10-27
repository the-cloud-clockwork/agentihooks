from pathlib import Path

import pytest

from scripts.ci_mutation.scope import changed_lines, select_tests


def test_hunks_include_only_added_and_changed_head_lines():
    diff = "@@ -3,2 +3,3 @@\n-old\n+new\n+extra\n@@ -12 +13 @@\n-old\n+new\n@@ -20,2 +21,0 @@\n-deleted\n"
    assert changed_lines(diff) == {3, 4, 5, 13}


@pytest.mark.parametrize(
    ("entry", "test", "expected"),
    [
        ("tests/a.py", "tests/a.py::t", True),
        ("tests/a.py", "tests/a.pyc::t", False),
        ("tests/a.py::t", "tests/a.py::t", True),
        ("tests/a.py::t", "tests/a.py::t[1]@port", True),
        ("tests/a.py::t", "tests/a.py::t@port", True),
        ("tests/a.py::t", "tests/a.py::tx", False),
        ("tests/a.py::t", "tests/b.py::t", False),
        ("tests/a.py::T", "tests/a.py::T::t", True),
        ("tests/a.py::t[a@b]", "tests/a.py::t[a@b]", True),
    ],
)
def test_a_selected_file_or_test_id_owns_its_parametrized_and_grouped_node_ids(entry, test, expected):
    from scripts.ci_mutation.scope import selects, test_file

    assert selects(entry, test) is expected
    assert test_file(test) == test.partition("::")[0]
    assert test_file("tests/a.py") == "tests/a.py"
    assert test_file("tests/a.py::T::t[1]") == "tests/a.py"


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


def test_selection_parses_only_test_modules_that_name_a_reached_module(tmp_path, monkeypatch):
    from scripts.ci_mutation import scope

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_direct.py").write_text("from hooks.context import parsed_sample\n")
    (tests / "parsed_helper.py").write_text("import hooks.context.parsed_sample\n")
    (tests / "test_helper_user.py").write_text("from tests import parsed_helper\n")
    for index in range(3):
        (tests / f"test_unrelated_{index}.py").write_text(f"import os\nUNRELATED = {index}\n")
    parsed = []
    parse = scope.ast.parse
    monkeypatch.setattr(scope.ast, "parse", lambda text, *args, **kwargs: parsed.append(text) or parse(text))
    scope.imported_names.cache_clear()
    scope.module_facts.cache_clear()
    assert select_tests(tmp_path, Path("hooks/context/parsed_sample.py")) == [
        "tests/test_direct.py",
        "tests/test_helper_user.py",
    ]
    assert parsed and not any("UNRELATED" in text for text in parsed)


@pytest.mark.parametrize(
    "importer",
    ["from.import *\n", "from..sub import *\n", "from\\\n. import *\n", "from hooks.context import ｐarsed_form\n"],
)
def test_selection_keeps_imports_written_without_spaces_or_with_normalized_names(tmp_path, importer):
    package = tmp_path / "tests" / "sub"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("import hooks.context.parsed_form\n")
    (package / "test_importer.py").write_text(importer)
    assert select_tests(tmp_path, Path("hooks/context/parsed_form.py")) == ["tests/sub/test_importer.py"]


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


def test_a_branch_that_merged_dev_and_edited_a_test_grades_only_its_own_lines_from_current_dev(tmp_path):
    import yaml

    from scripts.ci_mutation.scope import discover_changes, own_bases
    from tests.test_ci_hosted_lanes import _evaluate

    jobs = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/test.yml").read_text())["jobs"]
    plan = next(step for step in jobs["mutation-plan"]["steps"] if step.get("id") == "plan")
    assert jobs["mutation-plan"]["steps"][0]["with"]["fetch-depth"] == 0
    git, commit = _repo(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_a():\n    assert 1\n")
    git("add", "tests/test_x.py")
    pinned = commit("dev.py", "a = 1\n")
    git("checkout", "-q", "-b", "branch")
    commit("own.py", "def f():\n    return 1\n")
    (tmp_path / "tests" / "test_x.py").write_text("def test_a():\n    assert 2\n")
    git("commit", "-q", "-am", "edit test")
    git("checkout", "-q", "dev")
    commit("dev.py", "def d():\n    return 2\n")
    git("update-ref", "refs/remotes/origin/dev", "HEAD")
    merged_dev = git("rev-parse", "HEAD")
    git("checkout", "-q", "branch")
    git("merge", "-q", "--no-edit", "dev")
    assert discover_changes(tmp_path, [pinned, merged_dev], "HEAD") == {"hooks/own.py": {1, 2}, "hooks/dev.py": {1, 2}}
    event = {"pull_request": {"base": {"sha": pinned}}}
    bases = own_bases(tmp_path, _evaluate(plan["env"]["BASE"], event=event), "HEAD")
    assert bases == [merged_dev]
    assert discover_changes(tmp_path, bases, "HEAD") == {"hooks/own.py": {1, 2}}


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
        (
            "tests/test_x.py",
            "import os\nimport os\n" + PASSING,
            "import os\nimport os\n" + PASSING + "\n\ndef test_c():\n    pass\n",
            True,
        ),
        ("tests/test_x.py", PASSING, PASSING + "\n\nasync def test_c():\n    assert 3\n", True),
        ("tests/test_x.py", PASSING, PASSING + "\n\nclass TestC:\n    def test_c(self):\n        assert 3\n", True),
        ("tests/test_x.py", PASSING, "import os\n" + PASSING, False),
        ("tests/test_x.py", PASSING, "from os import path\n" + PASSING, False),
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
        ("tests/test_x.py", PASSING, PASSING + "\nimport os\n\n\ndef test_c():\n    assert os.sep\n", False),
        ("tests/test_x.py", None, "import os\n\n\ndef test_c():\n    assert os.sep\n", True),
        ("tests/test_x.py", None, "from os import path\n\n\ndef test_c():\n    assert path\n", True),
        ("tests/test_x.py", PASSING, PASSING + "\nfrom os.path import *\n", False),
        (
            "tests/test_x.py",
            PASSING + "\n\ndef test_c(repo):\n    assert repo\n",
            PASSING + "\n\ndef test_c(repo):\n    assert repo\n\nfrom os import path as repo\n",
            False,
        ),
        (
            "tests/test_x.py",
            PASSING + "\n\ndef test_c():\n    assert value\n",
            PASSING + "\n\ndef test_c():\n    assert value\n\nfrom os import sep as value\n",
            False,
        ),
        ("tests/test_x.py", PASSING, PASSING + "\nfrom os import sep as len\n", False),
        (
            "tests/test_x.py",
            PASSING + '\n\n@pytest.mark.usefixtures("thing")\ndef test_c():\n    pass\n',
            PASSING + '\n\n@pytest.mark.usefixtures("thing")\ndef test_c():\n    pass\n\nfrom os import sep as thing\n',
            False,
        ),
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

    calls = []

    def run(command, **kwargs):
        if "test.yml" in command[4]:
            return subprocess.CompletedProcess(command, 0, "", "")
        calls.append((command, kwargs))
        code, out = jobs[command[4].split("/")[5]] if len(calls) > 1 else runs
        return subprocess.CompletedProcess(command, code, out, "")

    monkeypatch.setattr(scope.subprocess, "run", run)
    assert scope.graded_green("abc") is expected
    api = ["gh", "api", "-X", "GET"]
    green = '[.jobs[] | select(.name == "mutation" and .conclusion == "success")] | length'
    listed = [
        *api,
        "repos/{owner}/{repo}/actions/workflows/mutation-preflight.yml/runs",
        "-f",
        "head_sha=abc",
        "-f",
        "event=push",
        "--jq",
        ".workflow_runs[].id",
    ]
    expected_calls = [listed] + [
        [*api, f"repos/{{owner}}/{{repo}}/actions/runs/{run}/jobs", "--jq", green] for run in asked
    ]
    assert calls == [(command, {"capture_output": True, "text": True}) for command in expected_calls]


@pytest.mark.parametrize(
    ("preflight", "runs", "jobs", "expected"),
    [
        ("", "9\n", {"9": "success\nsuccess\n"}, True),
        ("5\n", "9\n", {"9": "success\n"}, True),
        ("", "9\n", {"9": "success\nfailure\n"}, False),
        ("", "9\n", {"9": "failure\nsuccess\n"}, False),
        ("", "9\n", {"9": "success\nskipped\n"}, False),
        ("", "9\n", {"9": ""}, False),
        ("", "8\n9\n", {"8": "cancelled\n", "9": "success\n"}, True),
        ("", "", {}, False),
    ],
)
def test_an_open_branch_is_graded_by_its_pull_request_mutation_shards_when_its_push_preflight_skipped(
    monkeypatch, preflight, runs, jobs, expected
):
    import subprocess

    from scripts.ci_mutation import scope

    calls = []

    def run(command, **kwargs):
        calls.append(command)
        path = command[4]
        if "mutation-preflight.yml" in path:
            out = preflight
        elif "test.yml" in path:
            out = runs
        elif "--paginate" not in command:
            out = "0\n"
        else:
            out = jobs[path.split("/")[5]]
        return subprocess.CompletedProcess(command, 0, out, "")

    monkeypatch.setattr(scope.subprocess, "run", run)
    assert scope.graded_green("abc") is expected
    api = ["gh", "api", "-X", "GET"]
    shards = scope.SHARDS
    listed = [
        *api,
        "repos/{owner}/{repo}/actions/workflows/test.yml/runs",
        "-f",
        "head_sha=abc",
        "-f",
        "event=pull_request",
        "--jq",
        ".workflow_runs[].id",
    ]
    checked = len(preflight.split())
    assert calls[checked + 1] == listed
    asked = runs.split()[: runs.split().index("9") + 1] if expected else runs.split()
    jobs_listed = [
        [*api, f"repos/{{owner}}/{{repo}}/actions/runs/{run}/jobs", "-f", "filter=all", "--paginate", "--jq", shards]
        for run in asked
    ]
    assert calls[checked + 2 :] == jobs_listed


_INLINE = "Mutate the only shard in the statistics job"


@pytest.mark.parametrize(
    ("jobs", "expected"),
    [
        (
            [{"name": "mutation (0)", "conclusion": "success"}, {"name": "mutation (1)", "conclusion": "failure"}],
            ["success", "failure"],
        ),
        (
            [
                {
                    "name": "mutation-stats (0)",
                    "conclusion": "success",
                    "steps": [{"name": _INLINE, "conclusion": "success"}],
                },
                {"name": "mutation", "conclusion": "skipped"},
            ],
            ["success"],
        ),
        (
            [
                {
                    "name": "mutation-stats (0)",
                    "conclusion": "failure",
                    "steps": [{"name": _INLINE, "conclusion": "failure"}],
                }
            ],
            ["failure"],
        ),
        (
            [
                {
                    "name": "mutation-stats (0)",
                    "conclusion": "success",
                    "steps": [{"name": _INLINE, "conclusion": "skipped"}],
                },
                {"name": "mutation (0)", "conclusion": "success"},
            ],
            ["success"],
        ),
        (
            [
                {"name": "mutation-stats (1)", "conclusion": "success"},
                {"name": "mutation-plan", "conclusion": "success"},
            ],
            [],
        ),
        (
            [
                {
                    "name": "mutation-stats (0)",
                    "conclusion": "failure",
                    "steps": [{"name": _INLINE, "conclusion": "success"}, {"name": "Upload", "conclusion": "failure"}],
                }
            ],
            ["failure"],
        ),
    ],
)
def test_pull_request_grading_counts_the_shard_mutated_inside_the_statistics_job(jobs, expected):
    import json
    import subprocess

    from scripts.ci_mutation import scope

    found = subprocess.run(["jq", "-r", scope.SHARDS], input=json.dumps({"jobs": jobs}), capture_output=True, text=True)
    assert (found.returncode, found.stdout.split()) == (0, expected), found.stderr


@pytest.mark.parametrize("helper", ["oracle.py", "transitive", "fixture", "other.py", "dev.py"])
def test_inherited_lines_are_regraded_once_the_branch_changes_a_source_helper_their_tests_import(tmp_path, helper):
    from scripts.ci_mutation.scope import discover_changes

    git, commit = _repo(tmp_path)
    (tmp_path / "tests").mkdir()
    fixtures = {"transitive": "from hooks import dev\n", "fixture": "from hooks.oracle import ONE\n"}
    imports = {
        "transitive": "from hooks import oracle\nfrom tests.fixtures import dev\n",
        "fixture": "from hooks import dev\nfrom tests.fixtures import ONE as oracle_one\n",
    }
    if helper in fixtures:
        (tmp_path / "tests" / "fixtures.py").write_text(fixtures[helper])
    oracle = "oracle_one" if helper == "fixture" else "oracle.ONE"
    head = imports.get(helper, "from hooks import dev, oracle\n")
    (tmp_path / "tests" / "test_api.py").write_text(f"{head}\n\ndef test_d():\n    assert dev.d() == {oracle}\n")
    helper = "oracle.py" if helper in fixtures else helper
    (tmp_path / "tests" / "test_other.py").write_text("from hooks import other\n")
    git("add", "tests")
    commit("oracle.py", "ONE = 1\n")
    kept = "def d():\n    return 1\n\n\ndef k():\n    return 1\n"
    pinned = commit("dev.py", kept)
    git("checkout", "-q", "-b", "branch")
    if helper == "dev.py":
        commit(helper, kept + "\n\ndef g():\n    return 3\n")
    else:
        commit(helper, "ONE = 1\nTWO = 2\n")
    git("checkout", "-q", "dev")
    commit("dev.py", "def c():\n    return 0\n\n\n" + kept)
    git("update-ref", "refs/remotes/origin/dev", "HEAD")
    git("checkout", "-q", "branch")
    git("merge", "-q", "--no-edit", "dev")
    expected = {
        "oracle.py": {"hooks/oracle.py": {2}, "hooks/dev.py": {1, 2, 3, 4}},
        "other.py": {"hooks/other.py": {1, 2}},
        "dev.py": {"hooks/dev.py": {11, 12, 13, 14}},
    }
    assert discover_changes(tmp_path, pinned, "HEAD") == expected[helper]


def test_repeated_selection_scans_each_test_text_once(tmp_path):
    from scripts.ci_mutation import scope

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_first.py").write_text("from hooks.context import first_sample\n")
    (tests / "test_second.py").write_text("from hooks.context import second_sample\n")
    scope.mentions.cache_clear()
    assert select_tests(tmp_path, Path("hooks/context/first_sample.py")) == ["tests/test_first.py"]
    assert scope.mentions.cache_info().misses == 2
    assert select_tests(tmp_path, Path("hooks/context/second_sample.py")) == ["tests/test_second.py"]
    assert scope.mentions.cache_info().misses == 2


def test_a_word_inside_a_longer_identifier_does_not_mark_a_file_for_parsing(tmp_path, monkeypatch):
    from scripts.ci_mutation import scope

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_direct.py").write_text("from hooks.context import word_sample\n")
    (tests / "test_longer.py").write_text("word_sample_extra = 1\n")
    parsed = []
    parse = scope.ast.parse
    monkeypatch.setattr(scope.ast, "parse", lambda text, *args, **kwargs: parsed.append(text) or parse(text))
    scope.imported_names.cache_clear()
    scope.module_facts.cache_clear()
    scope.mentions.cache_clear()
    assert select_tests(tmp_path, Path("hooks/context/word_sample.py")) == ["tests/test_direct.py"]
    assert parsed == ["from hooks.context import word_sample\n"]


def test_a_file_naming_only_the_last_part_of_a_module_is_not_parsed(tmp_path, monkeypatch):
    from scripts.ci_mutation import scope

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_direct.py").write_text("from hooks.context import part_sample\n")
    (tests / "test_other.py").write_text("from scripts.other import part_sample\n")
    parsed = []
    parse = scope.ast.parse
    monkeypatch.setattr(scope.ast, "parse", lambda text, *args, **kwargs: parsed.append(text) or parse(text))
    scope.imported_names.cache_clear()
    scope.module_facts.cache_clear()
    scope.mentions.cache_clear()
    assert select_tests(tmp_path, Path("hooks/context/part_sample.py")) == ["tests/test_direct.py"]
    assert parsed == ["from hooks.context import part_sample\n"]


def test_a_text_naming_exactly_every_part_of_a_module_is_mentioned():
    from scripts.ci_mutation import scope

    assert scope.mentioned("hooks.exact_sample", [frozenset({"hooks", "exact_sample"})])
    assert not scope.mentioned("hooks.other", [frozenset({"hooks", "exact_sample"})])


def test_module_facts_names_every_import_and_nested_function_from_one_parse(monkeypatch):
    from scripts.ci_mutation import scope

    source = (
        "import os.path\n"
        "try:\n    from . import sibling\nexcept ImportError:\n    from .. import parent as p\n"
        "class TestGroup:\n    def test_one(self):\n        import json\n"
        "        def helper():\n            pass\n"
        "async def test_two():\n    match 1:\n        case 1:\n            from hooks import context\n"
    )
    parsed = []
    parse = scope.ast.parse
    monkeypatch.setattr(scope.ast, "parse", lambda text, *args, **kwargs: parsed.append(text) or parse(text))
    scope.imported_names.cache_clear()
    scope.module_facts.cache_clear()
    assert scope.imported_names(source, ("tests", "unit")) == {
        "os.path",
        "tests.unit",
        "tests.unit.sibling",
        "tests",
        "tests.parent",
        "json",
        "hooks",
        "hooks.context",
    }
    assert scope.module_facts(source)[1] == {"TestGroup.test_one", "TestGroup.test_one.helper", "test_two"}
    assert parsed == [source]
