import json
import subprocess
from pathlib import Path

import pytest

from scripts import no_comments


def _tree(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True)
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-f", *files], check=True)
    return root


def _grade(tmp_path: Path, before: str, after: str, name: str = "subject.py") -> int:
    base = _tree(tmp_path / "base", {name: before})
    head = _tree(tmp_path / "head", {name: after})
    return no_comments.main(["--base", str(base), "--head", str(head)])


def test_planted_comment_is_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _grade(tmp_path, "value = 1\n", "value = 1\n# planted comment\n") == 1
    assert capsys.readouterr().out == "::error::subject.py:2: forbidden comment\n"


@pytest.mark.parametrize(
    "directive",
    [
        "noqa",
        "noqa: F401,E501",
        "type: ignore",
        "type: ignore[attr-defined]",
        "pragma: no cover",
        "pragma: no branch",
        "pyright: ignore[reportAny]",
        "ruff: noqa: F401",
        "fmt: off",
        "fmt: on",
        "fmt: skip",
        "isort: skip",
        "isort: off",
        "isort: on",
        "nosec",
        "nosec B101",
        "NOSONAR",
        "nosemgrep",
        "nosemgrep: example.rule",
        "!/usr/bin/env python3",
        "coding: utf-8",
    ],
)
def test_tool_directives_are_green(tmp_path: Path, directive: str) -> None:
    assert _grade(tmp_path, "", f"#{directive}\nvalue = 1\n") == 0


@pytest.mark.parametrize(
    "directive",
    [
        "noqa explain this",
        "noqa: F401 because unused",
        "type: ignore because",
        "pragma: no cover explanation",
        "pyright: ignore reason",
        "ruff: noqa explanation",
        "fmt: off explanation",
        "isort: skip explanation",
        "nosec explanation",
        "NOSONAR explanation",
        "nosemgrep: rule explanation",
        "coding: utf-8 explanation",
    ],
)
def test_directive_prose_is_red(tmp_path: Path, directive: str) -> None:
    assert _grade(tmp_path, "", f"#{directive}\n") == 1


def test_docstrings_are_green(tmp_path: Path) -> None:
    source = '"""Module."""\nclass Subject:\n    """Class."""\n    def method(self) -> None:\n        """Method."""\n\nasync def action() -> None:\n    """Async function."""\n'
    assert _grade(tmp_path, "", source) == 0


@pytest.mark.parametrize("length, expected", [(20, 0), (21, 1)])
def test_module_docstring_limit(tmp_path: Path, length: int, expected: int) -> None:
    assert _grade(tmp_path, "", '"""\n' + "header\n" * (length - 2) + '"""\n') == expected


@pytest.mark.parametrize(
    "source", ['value = 1\n"prose"\n', 'if True:\n    "prose"\n', 'def action() -> None:\n    pass\n    "prose"\n']
)
def test_string_statements_are_red(tmp_path: Path, source: str) -> None:
    assert _grade(tmp_path, "", source) == 1


@pytest.mark.parametrize("folder, expected", [(".semgrep", 0), (".semgrep-tests/fixtures", 0), ("tests", 1)])
@pytest.mark.parametrize("annotation", ["ruleid:", "ok:", "todoruleid:", "todook:"])
def test_semgrep_annotations_are_scoped(tmp_path: Path, folder: str, expected: int, annotation: str) -> None:
    assert _grade(tmp_path, "", f"# {annotation} example.rule\n", f"{folder}/fixture.py") == expected


def test_unchanged_legacy_comment_is_green(tmp_path: Path) -> None:
    assert _grade(tmp_path, "# legacy\nvalue = 1\n", "# legacy\nvalue = 2\n") == 0


def test_changed_listed_file_must_remain_clean(tmp_path: Path) -> None:
    files = {"subject.py": "# legacy\nvalue = 1\n", "tests/COMMENT_FREE_PATHS.json": json.dumps(["subject.py"])}
    base = _tree(tmp_path / "base", files)
    head = _tree(tmp_path / "head", {**files, "subject.py": "# legacy\nvalue = 2\n"})
    assert no_comments.main(["--base", str(base), "--head", str(head)]) == 1


def test_registry_removal_is_red(tmp_path: Path) -> None:
    base = _tree(tmp_path / "base", {"subject.py": "pass\n", "tests/COMMENT_FREE_PATHS.json": '["subject.py"]'})
    head = _tree(tmp_path / "head", {"subject.py": "pass\n", "tests/COMMENT_FREE_PATHS.json": "[]"})
    assert no_comments.main(["--base", str(base), "--head", str(head)]) == 1


def test_whole_tree_checks_unchanged_comments(tmp_path: Path) -> None:
    root = _tree(tmp_path / "head", {"subject.py": "# legacy\n"})
    assert no_comments.main(["--head", str(root), "--all"]) == 1


def test_prepush_keeps_the_protected_grader_when_head_deletes_it(tmp_path: Path) -> None:
    from scripts import ci_prepush

    root = _tree(tmp_path / "repo", {"scripts/no_comments.py": "pass\n"})
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=CI", "-c", "user.email=ci@example.invalid", "commit", "-qm", "base"],
        check=True,
    )
    (root / "scripts/no_comments.py").unlink()
    grade = tmp_path / "grade"
    grade.mkdir()
    steps = ci_prepush._comment_step(root, "HEAD", grade)
    assert steps == [
        (
            "Python comments",
            [
                ci_prepush.sys.executable,
                "-I",
                str(grade / "comments-base/scripts/no_comments.py"),
                "--base",
                str(grade / "comments-base"),
                "--head",
                str(root),
            ],
        )
    ]
    assert (grade / "comments-base/scripts/no_comments.py").read_text() == "pass\n"
