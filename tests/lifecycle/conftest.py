import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from hooks.lifecycle.lease import admin_dir
from hooks.lifecycle.liveness import Snapshot
from hooks.lifecycle.model import Root
from hooks.proc import Process


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    admin = admin_dir(path)
    items = [path, *(item for item in path.rglob("*") if item.name != ".git")]
    if admin:
        items += [admin / "index", admin / "HEAD", admin / "logs" / "HEAD"]
    for item in items:
        if item.exists():
            os.utime(item, (stamp, stamp), follow_symlinks=False)


def snap(uptime: float = 99999.0, cwds: tuple[str, ...] = (), table=None, sessions=None) -> Snapshot:
    return Snapshot("boot-1", uptime, time.time(), table or {}, cwds, sessions or {})


def process(pid: int, *, ppid: int = 1, start: int = 100, comm: str = "claude", argv: tuple[str, ...] = ()) -> Process:
    return Process(pid, ppid, pid, pid, start, "S", comm, argv)


@dataclass
class Repo:
    origin: Path
    primary: Path
    trees: Path

    def worktree(self, name: str, start: str = "origin/dev", base: Path | None = None) -> Path:
        path = (base or self.trees / "primary") / name
        git(self.primary, "worktree", "add", "-q", "--no-track", "-b", name, str(path), start)
        return path

    def root(self) -> Root:
        return Root("worktrees", str(self.trees), "worktrees")


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Repo:
    for key, value in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }.items():
        monkeypatch.setenv(key, value)
    origin, primary = tmp_path / "origin.git", tmp_path / "primary"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "dev", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(primary)], check=True, capture_output=True)
    git(primary, "checkout", "-q", "-b", "dev")
    (primary / "README").write_text("x\n")
    git(primary, "add", "README")
    git(primary, "commit", "-q", "-m", "init")
    git(primary, "push", "-q", "origin", "dev")
    return Repo(origin, primary, tmp_path / "trees")
