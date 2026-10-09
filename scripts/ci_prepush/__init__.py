"""The cheap CI gates an agent runs before a push: lint, format, size limits and the touched tests.

A pass records HEAD in the worktree's git dir; hooks.context.prepush_guard refuses a push of any other HEAD."""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from scripts.ci_mutation.scope import select_tests

STAMP = "ci_prepush"
BASE = "origin/dev"
WORKERS = "2"
LINTED = ("hooks/", "scripts/", "tests/")
ALLOWLIST = "tests/SIZE_ALLOWLIST.json"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def stamp_path(root: Path) -> Path:
    return Path(_git(root, "rev-parse", "--absolute-git-dir")) / STAMP


def passed(root: Path) -> bool:
    try:
        path = stamp_path(root)
        return path.is_file() and path.read_text().strip() == _git(root, "rev-parse", "HEAD")
    except subprocess.CalledProcessError:
        return False


def changed(root: Path, base: str) -> list[str]:
    return _git(root, "diff", "--name-only", "--diff-filter=d", f"{base}...HEAD").splitlines()


def tests_for(root: Path, names: list[str]) -> list[str]:
    selected = set()
    for name in names:
        path = Path(name)
        if path.suffix != ".py":
            continue
        if path.parts[0] == "tests" and path.name.startswith("test_"):
            selected.add(name)
        selected.update(select_tests(root, path))
    return sorted(selected, key=lambda name: (Path(name).parent.as_posix(), name))


def plan(root: Path, base: str, grade: Path) -> list[tuple[str, list[str]]]:
    allowlist = grade / ALLOWLIST
    allowlist.parent.mkdir(parents=True, exist_ok=True)
    allowlist.write_text(_git(root, "show", f"{_git(root, 'merge-base', base, 'HEAD')}:{ALLOWLIST}") + "\n")
    python = sys.executable
    steps = [
        ("ruff check", [python, "-m", "ruff", "check", *LINTED]),
        ("ruff format", [python, "-m", "ruff", "format", "--check", *LINTED]),
        ("size limits", [python, "-m", "scripts.size_limits", "--base", str(grade), "--head", str(root)]),
    ]
    if tests := tests_for(root, changed(root, base)):
        steps.append(("tests", [python, "-m", "pytest", "-n", WORKERS, "--dist", "loadgroup", "-q", *tests]))
    return steps


def run(root: Path, base: str, execute=subprocess.run) -> int:
    if _git(root, "status", "--porcelain", "--untracked-files=no"):
        print("ci_prepush: commit or set aside the tracked changes first; the gates grade HEAD.")
        return 1
    env = {name: value for name, value in os.environ.items() if "REDIS" not in name}
    failed = []
    with tempfile.TemporaryDirectory() as grade:
        for name, command in plan(root, base, Path(grade)):
            print(f"ci_prepush: {name}", flush=True)
            if execute(command, cwd=root, env=env).returncode != 0:
                failed.append(name)
    if failed:
        print(f"ci_prepush: {', '.join(failed)} failed; fix them, commit and run again before pushing.")
        return 1
    head = _git(root, "rev-parse", "HEAD")
    stamp_path(root).write_text(head + "\n")
    print(f"ci_prepush: every cheap gate passed on {head[:12]}; push it.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.ci_prepush")
    parser.add_argument("--base", default=BASE)
    args = parser.parse_args(argv)
    return run(Path(_git(Path.cwd(), "rev-parse", "--show-toplevel")), args.base)
