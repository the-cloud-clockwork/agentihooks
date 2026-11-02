import fcntl
import os
import subprocess
import time
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


def _environments(home: Path) -> list[str]:
    return sorted(path.name for path in home.iterdir() if path.is_dir())


class _Uv:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []

    def __call__(self, command: list[str], check: bool, env: dict[str, str] | None = None) -> Any:
        assert check
        self.calls.append((command, env))
        if command[1] == "venv":
            Path(command[-1], "bin").mkdir(parents=True, exist_ok=True)
            Path(command[-1], "bin", "python").touch()
        return subprocess.CompletedProcess(command, 0)


@pytest.mark.parametrize("name", type_env.INPUTS)
def test_the_key_follows_every_input_that_decides_the_packages(tmp_path: Path, name: str) -> None:
    root = _root(tmp_path)
    before = type_env.key(root)

    (root / name).write_text("moved\n")

    assert type_env.key(root) != before


def test_the_environment_is_built_with_the_ci_install_command(tmp_path: Path) -> None:
    root = _root(tmp_path)
    home = tmp_path / "cache" / "home"
    uv = _Uv()

    python = type_env.environment(root, home, uv)

    target = home / type_env.key(root)
    assert python == target / "bin" / "python"
    assert uv.calls == [
        (["uv", "venv", "-q", "--clear", "--python", "3.12", str(target)], None),
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


def test_a_build_removes_only_environments_unused_for_a_week(tmp_path: Path) -> None:
    root = _root(tmp_path)
    home = tmp_path / "home"
    type_env.environment(root, home, _Uv())
    old = home / type_env.key(root)
    (root / type_env.LOCK).write_text("recent\n")
    type_env.environment(root, home, _Uv())
    recent = home / type_env.key(root)
    aged = time.time() - type_env.STALE_SECONDS - 60
    os.utime(old / type_env.READY, (aged, aged))
    (root / type_env.LOCK).write_text("moved\n")

    type_env.environment(root, home, _Uv())

    assert _environments(home) == sorted([recent.name, type_env.key(root)])


def test_a_build_keeps_only_the_most_recently_used_environments(tmp_path: Path) -> None:
    root = _root(tmp_path)
    home = tmp_path / "home"
    built = []
    for number in range(type_env.MAX_ENVIRONMENTS + 1):
        (root / type_env.LOCK).write_text(f"{number}\n")
        type_env.environment(root, home, _Uv())
        used = time.time() - 100 + number
        os.utime(home / type_env.key(root) / type_env.READY, (used, used))
        built.append(type_env.key(root))

    assert _environments(home) == sorted(built[1:])


def test_a_build_keeps_an_environment_whose_lock_another_run_holds(tmp_path: Path) -> None:
    root = _root(tmp_path)
    home = tmp_path / "home"
    type_env.environment(root, home, _Uv())
    held = home / type_env.key(root)
    aged = time.time() - type_env.STALE_SECONDS - 60
    os.utime(held / type_env.READY, (aged, aged))
    (root / type_env.LOCK).write_text("moved\n")

    with (home / f"{held.name}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        type_env.environment(root, home, _Uv())

    assert _environments(home) == sorted([held.name, type_env.key(root)])


def test_a_held_environment_does_not_stop_the_prune_of_older_ones(tmp_path: Path) -> None:
    root = _root(tmp_path)
    home = tmp_path / "home"
    aged = time.time() - type_env.STALE_SECONDS - 60
    built = []
    for number in range(2):
        (root / type_env.LOCK).write_text(f"{number}\n")
        type_env.environment(root, home, _Uv())
        built.append(home / type_env.key(root))
        os.utime(built[-1] / type_env.READY, (aged + number, aged + number))
    (root / type_env.LOCK).write_text("moved\n")

    with (home / f"{built[1].name}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        type_env.environment(root, home, _Uv())

    assert _environments(home) == sorted([built[1].name, type_env.key(root)])


def test_an_environment_removed_during_the_scan_does_not_stop_the_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    home = tmp_path / "home"
    type_env.environment(root, home, _Uv())
    old = home / type_env.key(root)
    aged = time.time() - type_env.STALE_SECONDS - 60
    os.utime(old / type_env.READY, (aged, aged))
    (root / type_env.LOCK).write_text("moved\n")
    gone = home / "gone"
    iterdir = Path.iterdir
    is_dir = Path.is_dir
    monkeypatch.setattr(Path, "iterdir", lambda self: iter([gone, *iterdir(self)]) if self == home else iterdir(self))
    monkeypatch.setattr(Path, "is_dir", lambda self: self == gone or is_dir(self))

    type_env.environment(root, home, _Uv())
    monkeypatch.undo()

    assert _environments(home) == [type_env.key(root)]


def test_a_build_keeps_an_environment_used_exactly_at_the_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    home = tmp_path / "home"
    type_env.environment(root, home, _Uv())
    edge = home / type_env.key(root)
    now = 10 * type_env.STALE_SECONDS
    os.utime(edge / type_env.READY, (now - type_env.STALE_SECONDS, now - type_env.STALE_SECONDS))
    monkeypatch.setattr(type_env.time, "time", lambda: now)
    (root / type_env.LOCK).write_text("moved\n")

    type_env.environment(root, home, _Uv())

    assert _environments(home) == sorted([edge.name, type_env.key(root)])


def test_reuse_marks_the_environment_used(tmp_path: Path) -> None:
    root = _root(tmp_path)
    home = tmp_path / "home"
    type_env.environment(root, home, _Uv())
    ready = home / type_env.key(root) / type_env.READY
    os.utime(ready, (0, 0))

    type_env.environment(root, home, _Uv())

    assert ready.stat().st_mtime > 0


def test_an_environment_without_its_interpreter_is_rebuilt(tmp_path: Path) -> None:
    root = _root(tmp_path)
    home = tmp_path / "home"
    (home / type_env.key(root) / "bin").mkdir(parents=True)
    (home / type_env.key(root) / type_env.READY).write_text("ready\n")
    uv = _Uv()

    type_env.environment(root, home, uv)

    assert [command[1] for command, _ in uv.calls] == ["venv", "pip"]


def test_main_runs_the_grader_with_the_environment_python_under_its_shared_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    python = tmp_path / "home" / "env" / "bin" / "python"
    python.parent.mkdir(parents=True)
    seen: list[tuple[list[str], Path]] = []

    def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
        with (tmp_path / "home" / "env.lock").open("a") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        seen.append((command, cwd))
        return subprocess.CompletedProcess(command, 3)

    monkeypatch.setattr(type_env, "environment", lambda given: python if given == root else None)
    monkeypatch.setattr(type_env.subprocess, "run", run)

    code = type_env.main(["--grader", str(tmp_path / "gate.py"), "--root", str(root), "--base", "b", "--head", "h"])

    assert code == 3
    assert seen == [([str(python), "-I", str(tmp_path / "gate.py"), "--base", "b", "--head", "h"], root)]


def test_main_defaults_to_the_current_repository_and_its_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _root(tmp_path.resolve())
    python = tmp_path / "home" / "env" / "bin" / "python"
    python.parent.mkdir(parents=True)
    seen: list[list[str]] = []
    monkeypatch.chdir(root)
    monkeypatch.setattr(type_env, "environment", lambda given: python if given == root else None)
    monkeypatch.setattr(
        type_env.subprocess,
        "run",
        lambda command, cwd: seen.append(command) or subprocess.CompletedProcess(command, 0),
    )

    assert type_env.main(["--write"]) == 0

    assert seen == [[str(python), "-I", str(root / "scripts" / "type_gate.py"), "--write"]]


def test_main_fails_when_the_environment_cannot_be_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(root: Path) -> Path:
        raise subprocess.CalledProcessError(2, ["uv"])

    monkeypatch.setattr(type_env, "environment", refuse)

    assert type_env.main(["--root", str(tmp_path)]) == 1
    assert "The CI type environment could not run the type gate" in capsys.readouterr().out
