"""Pod git for the lab worker: a private store over the node mirror and read only worktrees per head."""

import shutil
import stat
import subprocess
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Mirror", "Worktrees"]

WRITE = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True)


def _owned(tree: Path) -> list[Path]:
    return [path for path in (tree, *tree.rglob("*")) if not path.is_symlink()]


def _seal(tree: Path) -> None:
    for path in _owned(tree):
        path.chmod(path.stat().st_mode & ~WRITE)


def _unseal(tree: Path) -> None:
    for path in _owned(tree):
        path.chmod(path.stat().st_mode | stat.S_IWUSR)


@dataclass(frozen=True)
class Mirror:
    """The node's bare mirror, shared and read only, and the pod's private store that borrows its objects."""

    node: Path
    store: Path
    remote: str

    def fetch(self, sha: str) -> bool:
        """Make sha present in the store, fetching only its missing objects; True when anything was fetched."""
        if not self.store.exists():
            _git(self.store.parent, "init", "--quiet", "--bare", str(self.store))
            (self.store / "objects/info/alternates").write_text(f"{self.node / 'objects'}\n")
        found = subprocess.run(["git", "-C", str(self.store), "cat-file", "-t", sha], capture_output=True, text=True)
        if found.stdout == "commit\n":
            return False
        _git(self.store, "fetch", "--no-write-fetch-head", self.remote, sha)
        return True


@dataclass
class Worktrees:
    """Read only worktrees per head shared by that head's units, evicted least recently leased once idle."""

    mirror: Mirror
    root: Path
    capacity: int
    live: OrderedDict[str, int] = field(default_factory=OrderedDict)

    def lease(self, head: str) -> Path:
        """Return the head's shared tree, cutting it on first use, and count one more live unit on it."""
        tree = self.root / head
        if head not in self.live:
            self.mirror.fetch(head)
            if tree.exists():
                self.drop(tree)
            _git(self.mirror.store, "worktree", "add", "--quiet", str(tree), head)
            _seal(tree)
            self.live[head] = 0
        self.live[head] += 1
        self.live.move_to_end(head)
        self._evict()
        return tree

    def release(self, head: str) -> None:
        """Count one live unit off the head's tree."""
        self.live[head] -= 1
        self._evict()

    def own(self, head: str) -> Path:
        """Cut a private writable worktree of head for a mutation unit."""
        self.mirror.fetch(head)
        self.root.mkdir(parents=True, exist_ok=True)
        tree = Path(tempfile.mkdtemp(prefix=f"{head}.", dir=self.root))
        _git(self.mirror.store, "worktree", "add", "--quiet", str(tree), head)
        return tree

    def drop(self, tree: Path) -> None:
        """Remove a worktree directory, registered or not, and prune the store's registrations."""
        _unseal(tree)
        shutil.rmtree(tree)
        _git(self.mirror.store, "worktree", "prune")

    def _evict(self) -> None:
        idle = [head for head, units in self.live.items() if units == 0]
        for head in idle[: max(len(self.live) - self.capacity, 0)]:
            del self.live[head]
            self.drop(self.root / head)
