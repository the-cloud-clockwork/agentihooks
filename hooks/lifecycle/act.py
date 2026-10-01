import gzip
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from hooks.lifecycle.gitstate import git
from hooks.lifecycle.lease import admin_dir
from hooks.lifecycle.model import Finding

SNAPSHOT_FILE_LIMIT = 50 << 20
PROTECTED = {"dev", "main", "master"}
IDENTITY = {"GIT_AUTHOR_NAME": "agentihooks gc", "GIT_AUTHOR_EMAIL": "gc@agentihooks.local"}


class ActionError(RuntimeError):
    pass


def _run(path: Path, *args: str, env: dict | None = None, timeout: int = 300) -> str:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(path), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **(env or {})},
    )
    if result.returncode != 0:
        raise ActionError(f"git {args[0]}: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def _oversized(path: Path) -> list[str]:
    listed = _run(path, "ls-files", "-z", "--modified", "--others", "--exclude-standard")
    big = []
    for name in filter(None, listed.split("\0")):
        try:
            if (path / name).stat().st_size > SNAPSHOT_FILE_LIMIT:
                big.append(name)
        except OSError:
            continue
    return big


def _snapshot_commit(path: Path, admin: Path, skipped: list[str]) -> str:
    index = admin / "agentihooks-snapshot.index"
    env = {"GIT_INDEX_FILE": str(index), **{k: os.environ.get(k, v) for k, v in IDENTITY.items()}}
    env.update({"GIT_COMMITTER_NAME": env["GIT_AUTHOR_NAME"], "GIT_COMMITTER_EMAIL": env["GIT_AUTHOR_EMAIL"]})
    try:
        _run(path, "read-tree", "HEAD", env=env)
        _run(path, "add", "-A", "--", ".", *(f":(exclude,literal){name}" for name in skipped), env=env)
        tree = _run(path, "write-tree", env=env)
    finally:
        index.unlink(missing_ok=True)
    lines = [f"wip: snapshot of {path.name} by agentihooks gc [skip ci]", ""]
    lines += ["Ignored files are not included."] + [f"skipped over 50 MB: {name}" for name in skipped]
    return _run(path, "commit-tree", tree, "-p", "HEAD", "-m", "\n".join(lines), env=env)


def snapshot(path: Path) -> str:
    admin = admin_dir(path)
    if admin is None:
        raise ActionError("not a linked worktree")
    commit = _snapshot_commit(path, admin, _oversized(path))
    ref = f"wip/{admin.parent.parent.parent.name}/{path.name}-{time.strftime('%Y%m%d%H%M%S')}"
    _run(path, "push", "--no-follow-tags", "origin", f"{commit}:refs/heads/{ref}", timeout=600)
    return ref


def _drop_branch(primary: Path, branch: str) -> None:
    if not branch or branch in PROTECTED:
        return
    if git(primary, "rev-list", "-n1", branch, "--not", "--remotes").stdout.strip():
        return
    git(primary, "branch", "-D", branch)


def remove_worktree(path: Path) -> None:
    admin = admin_dir(path)
    if admin is None:
        raise ActionError("not a linked worktree")
    primary = admin.parent.parent.parent
    branch = git(path, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip() if path.exists() else ""
    if git(primary, "worktree", "remove", "--force", "--force", str(path)).returncode != 0:
        shutil.rmtree(path, ignore_errors=True)
    git(primary, "worktree", "prune")
    _drop_branch(primary, branch)


def archive_file(path: Path) -> None:
    target_dir = path.parent / "archive"
    target_dir.mkdir(exist_ok=True)
    target = target_dir / f"{path.name}.gz"
    if target.exists():
        target = target_dir / f"{path.name}.{time.strftime('%Y%m%d%H%M%S')}.gz"
    with open(path, "rb") as source, gzip.open(target, "wb") as sink:
        shutil.copyfileobj(source, sink)
    path.unlink()


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


class Journal:
    def __init__(self, file: Path):
        self.file = file

    def pending(self) -> dict[str, str]:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, str]) -> None:
        self.file.write_text(json.dumps(data), encoding="utf-8")

    def begin(self, path: str, category: str) -> None:
        self._write({**self.pending(), path: category})

    def end(self, path: str) -> None:
        data = self.pending()
        data.pop(path, None)
        self._write(data)


def finish_pending(journal: Journal) -> list[str]:
    finished = []
    for path, category in journal.pending().items():
        target = Path(path)
        if category == "worktree" and admin_dir(target) is not None:
            remove_worktree(target)
        elif target.exists():
            remove_path(target)
        journal.end(path)
        finished.append(path)
    return finished


def apply(item: Finding, journal: Journal) -> str:
    path = Path(item.path)
    if item.category == "worktree":
        note = ""
        if item.action == "snapshot":
            note = f"pushed {snapshot(path)}; "
        journal.begin(item.path, "worktree")
        remove_worktree(path)
        journal.end(item.path)
        return note + "removed"
    if item.action == "archive":
        archive_file(path)
        return "archived"
    journal.begin(item.path, item.category)
    remove_path(path)
    journal.end(item.path)
    return "removed"
