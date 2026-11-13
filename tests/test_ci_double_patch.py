from pathlib import Path

import pytest
import yaml

from scripts.ci_double_patch import main

_MOCKED_FIXTURE = (
    "import pytest\n"
    "from unittest.mock import patch\n"
    "@pytest.fixture(autouse=True)\n"
    "def _isolate():\n"
    "    with patch('pkg.mod.get_redis', return_value=None):\n"
    "        yield\n"
)
_MONKEYPATCH_TEST = "def test_one(monkeypatch):\n    monkeypatch.setattr('pkg.mod.get_redis', lambda: None)\n"


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "tree"
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _error(path: str, line: int, function: str, target: str, fixture: str, at: str) -> str:
    return (
        f"::error::{path}:{line}: {function} monkeypatches {target}, which autouse fixture {fixture} patches with "
        f"mock.patch at {at}; patch it through monkeypatch in that fixture so one stack unwinds both"
    )


def test_a_monkeypatch_of_a_target_its_module_autouse_fixture_mocks_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path, {"tests/test_one.py": _MOCKED_FIXTURE + _MONKEYPATCH_TEST})
    assert main(["--root", str(root)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "Double patches: 1 files, 1 problems",
        _error("tests/test_one.py", 8, "test_one", "pkg.mod.get_redis", "_isolate", "tests/test_one.py:5"),
    ]


def test_a_conftest_autouse_patch_object_reaches_a_nested_test_through_aliases(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(
        tmp_path,
        {
            "tests/conftest.py": (
                "import pytest\n"
                "from unittest import mock\n"
                "from pkg import mod\n"
                "@pytest.fixture(autouse=True)\n"
                "def _isolate():\n"
                "    patcher = mock.patch.object(mod, 'get_redis')\n"
                "    patcher.start()\n"
                "    yield\n"
                "    patcher.stop()\n"
            ),
            "tests/area/test_two.py": (
                "import pkg.mod as fake\n"
                "class TestTwo:\n"
                "    def test_two(self, monkeypatch):\n"
                "        monkeypatch.setattr(fake, 'get_redis', lambda: None)\n"
            ),
        },
    )
    assert main(["--root", str(root)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "Double patches: 2 files, 1 problems",
        _error("tests/area/test_two.py", 4, "TestTwo.test_two", "pkg.mod.get_redis", "_isolate", "tests/conftest.py:6"),
    ]


def test_the_nearest_autouse_fixture_is_named(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(
        tmp_path,
        {
            "tests/conftest.py": _MOCKED_FIXTURE.replace("_isolate", "_outer"),
            "tests/area/conftest.py": _MOCKED_FIXTURE.replace("_isolate", "_inner"),
            "tests/area/test_three.py": _MONKEYPATCH_TEST,
        },
    )
    assert main(["--root", str(root)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "Double patches: 3 files, 1 problems",
        _error("tests/area/test_three.py", 2, "test_one", "pkg.mod.get_redis", "_inner", "tests/area/conftest.py:5"),
    ]


def test_a_root_conftest_autouse_fixture_reaches_the_tests(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path, {"conftest.py": _MOCKED_FIXTURE, "tests/test_five.py": _MONKEYPATCH_TEST})
    assert main(["--root", str(root)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "Double patches: 1 files, 1 problems",
        _error("tests/test_five.py", 2, "test_one", "pkg.mod.get_redis", "_isolate", "conftest.py:5"),
    ]


def test_two_double_patches_exit_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(
        tmp_path,
        {"tests/test_six.py": _MOCKED_FIXTURE + _MONKEYPATCH_TEST + _MONKEYPATCH_TEST.replace("test_one", "test_two")},
    )
    assert main(["--root", str(root)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "Double patches: 1 files, 2 problems",
        _error("tests/test_six.py", 8, "test_one", "pkg.mod.get_redis", "_isolate", "tests/test_six.py:5"),
        _error("tests/test_six.py", 10, "test_two", "pkg.mod.get_redis", "_isolate", "tests/test_six.py:5"),
    ]


def test_an_undecodable_file_is_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path, {})
    (root / "tests").mkdir(parents=True)
    (root / "tests/test_bytes.py").write_bytes(b"\xff\xfe\x00")
    assert main(["--root", str(root)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Double patches: 1 files, 1 problems"
    assert out[1].startswith("::error::tests/test_bytes.py: cannot parse: ")


def test_a_fixture_that_monkeypatches_under_its_own_mock_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(
        tmp_path,
        {
            "tests/test_four.py": (
                "import pytest\n"
                "import unittest.mock\n"
                "@pytest.fixture(autouse=True)\n"
                "async def _isolate(monkeypatch):\n"
                "    with unittest.mock.patch('pkg.mod.HOME'):\n"
                "        monkeypatch.setattr('pkg.mod.HOME', '/tmp')\n"
                "        yield\n"
            )
        },
    )
    assert main(["--root", str(root)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "Double patches: 1 files, 1 problems",
        _error("tests/test_four.py", 6, "_isolate", "pkg.mod.HOME", "_isolate", "tests/test_four.py:5"),
    ]


def test_a_fixture_decorator_named_through_several_modules_is_autouse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = _MOCKED_FIXTURE.replace("pytest.fixture", "_pytest.fixtures.fixture")
    root = _tree(tmp_path, {"tests/test_seven.py": fixture + _MONKEYPATCH_TEST})
    assert main(["--root", str(root)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "Double patches: 1 files, 1 problems",
        _error("tests/test_seven.py", 8, "test_one", "pkg.mod.get_redis", "_isolate", "tests/test_seven.py:5"),
    ]


def test_a_module_fixture_patching_an_aliased_submodule_reaches_a_later_class(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(
        tmp_path,
        {
            "tests/test_eight.py": (
                "import pytest\n"
                "import pkg as p\n"
                "from unittest import mock\n"
                "@pytest.fixture(autouse=True)\n"
                "def _isolate():\n"
                "    with mock.patch.object(p.mod, 'get_redis'):\n"
                "        yield\n"
                "class TestEight:\n"
                "    def test_eight(self, monkeypatch):\n"
                "        monkeypatch.setattr('pkg.mod.get_redis', lambda: None)\n"
            )
        },
    )
    assert main(["--root", str(root)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "Double patches: 1 files, 1 problems",
        _error(
            "tests/test_eight.py", 10, "TestEight.test_eight", "pkg.mod.get_redis", "_isolate", "tests/test_eight.py:6"
        ),
    ]


@pytest.mark.parametrize(
    ("files", "graded"),
    [
        (
            {
                "tests/test_clean.py": (
                    "import pytest\n"
                    "@pytest.fixture(autouse=True)\n"
                    "def _isolate(monkeypatch):\n"
                    "    monkeypatch.setattr('pkg.mod.get_redis', lambda: None)\n"
                    "def test_one(monkeypatch):\n"
                    "    monkeypatch.setattr('pkg.mod.get_redis', lambda: 1)\n"
                )
            },
            1,
        ),
        (
            {
                "tests/test_clean.py": _MOCKED_FIXTURE
                + "def test_one(monkeypatch):\n    monkeypatch.setattr('pkg.mod.other', lambda: None)\n"
            },
            1,
        ),
        ({"tests/test_clean.py": _MOCKED_FIXTURE.replace("autouse=True", "autouse=False") + _MONKEYPATCH_TEST}, 1),
        ({"tests/test_clean.py": _MOCKED_FIXTURE.replace("autouse=True", "autouse=None") + _MONKEYPATCH_TEST}, 1),
        ({"tests/test_clean.py": _MOCKED_FIXTURE.replace("with patch(", "with helper(") + _MONKEYPATCH_TEST}, 1),
        (
            {
                "tests/test_clean.py": _MOCKED_FIXTURE.replace("@pytest.fixture(autouse=True)", "@pytest.fixture")
                + _MONKEYPATCH_TEST
            },
            1,
        ),
        (
            {
                "tests/test_clean.py": _MOCKED_FIXTURE.replace("pytest.fixture", "pytest.mark.usefixtures")
                + _MONKEYPATCH_TEST
            },
            1,
        ),
        (
            {
                "docs/conftest.py": _MOCKED_FIXTURE,
                "tests/other/conftest.py": _MOCKED_FIXTURE,
                "tests/area/test_clean.py": _MONKEYPATCH_TEST,
            },
            2,
        ),
        (
            {
                "tests/test_clean.py": (
                    "import pytest\n"
                    "from unittest.mock import patch\n"
                    "@pytest.fixture(autouse=True)\n"
                    "def _isolate():\n"
                    "    with patch.dict('os.environ', {}), patch.object(make().mod, 'x'), patch.object(make(), 'y'):\n"
                    "        yield\n"
                    "def test_one(monkeypatch, other):\n"
                    "    other.setattr('pkg.mod.get_redis', lambda: None)\n"
                    "    monkeypatch.setattr(make().mod, 'x', 1)\n"
                    "    monkeypatch.setattr(make(), 'y', 1)\n"
                    "    monkeypatch.setenv('HOME', '/tmp')\n"
                )
            },
            1,
        ),
        (
            {
                "tests/test_clean.py": _MOCKED_FIXTURE.replace("pkg.mod.get_redis", "XXXX")
                + "def test_one(monkeypatch, other):\n"
                "    monkeypatch.setenv('HOME', '/tmp')\n"
                "    other.setattr('XXXX', 1)\n"
            },
            1,
        ),
    ],
    ids=[
        "monkeypatch-fixture",
        "other-target",
        "autouse-false",
        "autouse-none",
        "not-a-mock",
        "plain-fixture",
        "not-a-fixture",
        "conftests-off-the-path",
        "unnamed-owners",
        "calls-that-are-not-monkeypatch-setattr",
    ],
)
def test_clean_forms_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], files: dict[str, str], graded: int
) -> None:
    root = _tree(tmp_path, files)
    assert main(["--root", str(root)]) == 0
    assert capsys.readouterr().out.splitlines() == [f"Double patches: {graded} files, 0 problems"]


def test_an_empty_tree_and_an_unparseable_file_are_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--root", str(tmp_path)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "Double patches: 0 files, 1 problems",
        "::error::No Python test files found",
    ]
    root = _tree(tmp_path, {"tests/test_broken.py": "def broken(:\n"})
    assert main(["--root", str(root)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Double patches: 1 files, 1 problems"
    assert out[1].startswith("::error::tests/test_broken.py: cannot parse: ")


def test_lint_grades_the_head_tree_with_the_protected_grader() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/test.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["lint"]["steps"]
        if step.get("name") == "Refuse a monkeypatch of a target an autouse fixture mocks"
    )
    assert step["if"] == "${{ !cancelled() }}"
    assert 'cd "$RUNNER_TEMP/grader"' in step["run"]
    assert 'python -m scripts.ci_double_patch --root "$GITHUB_WORKSPACE"' in step["run"]
