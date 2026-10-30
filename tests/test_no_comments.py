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
        "pragma: no mutate",
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


@pytest.mark.parametrize(
    "source", ["  # coding: utf-8\nvalue = 1\n", "\n\t# coding: utf-8\nvalue = 1\n", "\f# coding: utf-8\nvalue = 1\n"]
)
def test_indented_encoding_cookies_are_green(tmp_path: Path, source: str) -> None:
    assert _grade(tmp_path, "", source) == 0


@pytest.mark.parametrize("name", ["subject.py", "nested/.semgrep/fixtures/subject.py"])
def test_new_clean_registration_checks_the_entire_file(tmp_path: Path, name: str) -> None:
    files = {name: "# legacy\nvalue = 1\n", "tests/COMMENT_FREE_PATHS.json": "[]"}
    base = _tree(tmp_path / "base", files)
    head = _tree(tmp_path / "head", {**files, "tests/COMMENT_FREE_PATHS.json": json.dumps([name])})
    assert no_comments.grade(base, head, False) == [f"{name}:1: forbidden comment"]


def test_clean_registry_growth_is_green(tmp_path: Path) -> None:
    files = {"subject.py": "pass\n", "tests/COMMENT_FREE_PATHS.json": "[]"}
    base = _tree(tmp_path / "base", files)
    head = _tree(tmp_path / "head", {**files, "tests/COMMENT_FREE_PATHS.json": '["subject.py"]'})
    assert no_comments.grade(base, head, False) == []


@pytest.mark.parametrize("value", [{}, ["../subject.py"], ["/subject.py"], ["subject.txt"], [1]])
def test_invalid_registry_is_red(tmp_path: Path, value: object, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path / "head", {"subject.py": "pass\n", "tests/COMMENT_FREE_PATHS.json": json.dumps(value)})
    assert no_comments.main(["--head", str(root), "--all"]) == 1
    reason = (
        "must be a list of paths" if value == {} or value == [1] else "must contain repository relative Python paths"
    )
    assert capsys.readouterr().out == f"::error::cannot grade Python comments: tests/COMMENT_FREE_PATHS.json {reason}\n"


def test_untracked_registration_is_red(tmp_path: Path) -> None:
    root = _tree(tmp_path / "head", {"tests/COMMENT_FREE_PATHS.json": '["absent.py"]'})
    assert no_comments.grade(None, root, True) == [
        "tests/COMMENT_FREE_PATHS.json: comment free path is not tracked: absent.py"
    ]


def test_missing_tracked_python_is_red(tmp_path: Path) -> None:
    root = _tree(tmp_path / "head", {"subject.py": "pass\n", "a.txt": "data\n"})
    (root / "subject.py").unlink()
    (root / "a.txt").unlink()
    assert no_comments.grade(None, root, True) == ["subject.py: tracked Python file is missing"]


def test_non_python_and_unchanged_files_do_not_end_the_walk(tmp_path: Path) -> None:
    files = {"a.txt": "data\n", "b.py": "pass\n", "z.py": "pass\n"}
    base = _tree(tmp_path / "base", files)
    head = _tree(tmp_path / "head", {**files, "z.py": "# plant\n"})
    assert no_comments.grade(base, head, False) == ["z.py:1: forbidden comment"]


def test_unmodified_files_are_not_parsed(tmp_path: Path) -> None:
    files = {"subject.py": "this is invalid Python!\n"}
    base = _tree(tmp_path / "base", files)
    head = _tree(tmp_path / "head", files)
    assert no_comments.grade(base, head, False) == []


def test_unchanged_registered_file_is_not_regraded(tmp_path: Path) -> None:
    files = {"subject.py": "# legacy\n", "tests/COMMENT_FREE_PATHS.json": '["subject.py"]'}
    base = _tree(tmp_path / "base", files)
    head = _tree(tmp_path / "head", files)
    assert no_comments.grade(base, head, False) == []


def test_changes_before_an_existing_comment_are_green(tmp_path: Path) -> None:
    assert _grade(tmp_path, "value = 1\n# legacy\n", "value = 2\n# legacy\n") == 0


def test_new_file_uses_an_empty_base(tmp_path: Path) -> None:
    base = _tree(tmp_path / "base", {"old.py": "pass\n"})
    head = _tree(tmp_path / "head", {"old.py": "pass\n", "new.py": "# plant\n"})
    assert no_comments.grade(base, head, False) == ["new.py:1: forbidden comment"]


def test_discovery_is_exact_and_fails_outside_git(tmp_path: Path) -> None:
    root = _tree(tmp_path / "head", {"a.py": "pass\n", "line\nbreak.py": "pass\n", "z.txt": "data\n"})
    assert no_comments._tracked(root) == ["a.py", "line\nbreak.py", "z.txt"]
    with pytest.raises(subprocess.CalledProcessError):
        no_comments._tracked(tmp_path)


def test_unchanged_diff_has_no_lines(tmp_path: Path) -> None:
    old = tmp_path / "old.py"
    new = tmp_path / "new.py"
    old.write_text("pass\n")
    new.write_text("pass\n")
    assert no_comments._changed(old, new) == set()


def test_failed_diff_is_not_a_pass(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"git diff failed for absent.py:"):
        no_comments._changed(tmp_path / "old.py", tmp_path / "absent.py")


def test_cli_help_and_required_base(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as help_exit:
        no_comments.main(["--help"])
    assert help_exit.value.code == 0
    assert "Reject new Python comments and preserve comment free files." in capsys.readouterr().out.splitlines()
    with pytest.raises(SystemExit) as missing_exit:
        no_comments.main([])
    assert missing_exit.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1].endswith("error: --base is required unless --all is set")


def test_cli_defaults_to_current_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _tree(tmp_path / "head", {"subject.py": "# plant\n"})
    monkeypatch.chdir(root)
    assert no_comments.main(["--all"]) == 1


def test_cookie_after_the_second_line_is_red(tmp_path: Path) -> None:
    assert _grade(tmp_path, "", "\n\n# coding: utf-8\n") == 1


def test_numeric_expression_is_not_a_comment(tmp_path: Path) -> None:
    assert _grade(tmp_path, "", "42\n") == 0


def test_module_docstring_diagnostic() -> None:
    source = '"""\n' + "header\n" * 19 + '"""\n'
    assert no_comments.violations(source, "subject.py") == [(1, 21, "module docstring exceeds 20 lines")]


def test_nested_semgrep_fixture_directive_is_green(tmp_path: Path) -> None:
    assert _grade(tmp_path, "", "# ruleid: example.rule\n", "tests/.semgrep/fixtures/subject.py") == 0


def test_prepush_bootstraps_head_and_is_wired_into_plan(tmp_path: Path) -> None:
    from scripts import ci_prepush

    root = _tree(
        tmp_path / "repo",
        {
            ci_prepush.ALLOWLIST: "{}",
            ci_prepush.GRADER: "pass\n",
            "subject.py": "pass\n",
        },
    )
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=CI", "-c", "user.email=ci@example.invalid", "commit", "-qm", "base"],
        check=True,
    )
    (root / "scripts/no_comments.py").write_text("pass\n")
    grade = tmp_path / "grade"
    grade.mkdir()
    steps = dict(ci_prepush.plan(root, "HEAD", grade))
    assert steps["Python comments"] == [
        ci_prepush.sys.executable,
        "-I",
        str(root / "scripts/no_comments.py"),
        "--base",
        str(grade / "comments-base"),
        "--head",
        str(root),
    ]


def test_prepush_rejects_a_failed_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import ci_prepush

    root = _tree(tmp_path / "repo", {"scripts/no_comments.py": "pass\n"})
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=CI", "-c", "user.email=ci@example.invalid", "commit", "-qm", "base"],
        check=True,
    )
    wrapper = tmp_path / "bin/git"
    wrapper.parent.mkdir()
    wrapper.write_text('#!/usr/bin/env bash\n/usr/bin/git "$@"\nif [[ " $* " == *" archive "* ]]; then exit 23; fi\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{wrapper.parent}:{ci_prepush.os.environ['PATH']}")
    grade = tmp_path / "grade"
    grade.mkdir()
    with pytest.raises(subprocess.CalledProcessError) as failure:
        ci_prepush._comment_step(root, "HEAD", grade)
    assert failure.value.returncode == 23


def test_prepush_refuses_an_archive_link_outside_the_snapshot(tmp_path: Path) -> None:
    import tarfile

    from scripts import ci_prepush

    root = _tree(tmp_path / "repo", {"scripts/no_comments.py": "pass\n"})
    (root / "outside").symlink_to("/outside")
    subprocess.run(["git", "-C", str(root), "add", "outside"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=CI", "-c", "user.email=ci@example.invalid", "commit", "-qm", "base"],
        check=True,
    )
    grade = tmp_path / "grade"
    grade.mkdir()
    with pytest.raises(tarfile.AbsoluteLinkError):
        ci_prepush._comment_step(root, "HEAD", grade)


def test_whole_tree_includes_unchanged_base_files(tmp_path: Path) -> None:
    files = {"subject.py": "# legacy\n"}
    base = _tree(tmp_path / "base", files)
    head = _tree(tmp_path / "head", files)
    assert no_comments.grade(base, head, True) == ["subject.py:1: forbidden comment"]
