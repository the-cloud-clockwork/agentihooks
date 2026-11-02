import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import type_env


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".github").mkdir(parents=True)
    for name in type_env.INPUTS:
        (root / name).write_text(f"{name}\n")
    return root


class _Uv:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []

    def __call__(self, command: list[str], check: bool, env: dict[str, str] | None = None) -> Any:
        assert check
        self.calls.append((command, env))
        if command[1] == "venv":
            Path(command[-1], "bin").mkdir(parents=True)
        return subprocess.CompletedProcess(command, 0)


@pytest.mark.parametrize("name", type_env.INPUTS)
def test_the_key_follows_every_input_that_decides_the_packages(tmp_path: Path, name: str) -> None:
    root = _root(tmp_path)
    before = type_env.key(root)

    (root / name).write_text("moved\n")

    assert type_env.key(root) != before


def test_the_environment_is_built_with_the_ci_install_command(tmp_path: Path) -> None:
    root = _root(tmp_path)
    home = tmp_path / "home"
    uv = _Uv()

    python = type_env.environment(root, home, uv)

    target = home / type_env.key(root)
    assert python == target / "bin" / "python"
    assert uv.calls == [
        (["uv", "venv", "-q", "--python", "3.12", str(target)], None),
        (
            [
                "uv",
                "pip",
                "install",
                "-q",
                "--python",
                str(python),
                "--constraint",
                str(root / ".github/test-requirements.lock"),
                "--excludes",
                str(root / ".github/test-excludes.txt"),
                "-e",
                f"{root}[dev,all]",
            ],
            uv.calls[1][1],
        ),
    ]
    assert uv.calls[1][1] is not None
    assert uv.calls[1][1]["UV_CONSTRAINT"] == str(root / ".github/test-constraints.txt")
    assert (target / type_env.READY).is_file()


def test_a_built_environment_is_reused(tmp_path: Path) -> None:
    root = _root(tmp_path)
    home = tmp_path / "home"
    type_env.environment(root, home, _Uv())
    uv = _Uv()

    type_env.environment(root, home, uv)

    assert uv.calls == []


def test_a_lock_change_replaces_the_older_environment(tmp_path: Path) -> None:
    root = _root(tmp_path)
    home = tmp_path / "home"
    type_env.environment(root, home, _Uv())
    old = type_env.key(root)
    (root / type_env.LOCK).write_text("moved\n")

    type_env.environment(root, home, _Uv())

    assert sorted(path.name for path in home.iterdir()) == [type_env.key(root)]
    assert type_env.key(root) != old


def test_an_unfinished_build_is_rebuilt(tmp_path: Path) -> None:
    root = _root(tmp_path)
    home = tmp_path / "home"
    (home / type_env.key(root) / "bin").mkdir(parents=True)
    uv = _Uv()

    type_env.environment(root, home, uv)

    assert [command[1] for command, _ in uv.calls] == ["venv", "pip"]


def test_main_runs_the_grader_with_the_environment_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _root(tmp_path)
    python = tmp_path / "env/bin/python"
    seen: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(type_env, "environment", lambda given: python if given == root else None)
    monkeypatch.setattr(
        type_env.subprocess,
        "run",
        lambda command, cwd: seen.append((command, cwd)) or subprocess.CompletedProcess(command, 3),
    )

    code = type_env.main(["--grader", str(tmp_path / "gate.py"), "--root", str(root), "--base", "b", "--head", "h"])

    assert code == 3
    assert seen == [([str(python), "-I", str(tmp_path / "gate.py"), "--base", "b", "--head", "h"], root)]


def test_main_fails_when_the_environment_cannot_be_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(root: Path) -> Path:
        raise subprocess.CalledProcessError(2, ["uv"])

    monkeypatch.setattr(type_env, "environment", refuse)

    assert type_env.main(["--root", str(tmp_path)]) == 1
    assert "The CI type environment could not be built" in capsys.readouterr().out
