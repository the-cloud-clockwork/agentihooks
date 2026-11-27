"""Pod git for the lab worker: a private store over the node mirror and read only worktrees per head."""

import base64
import os
import shutil
import stat
import subprocess
import tempfile
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from scripts.ci_lab.remote import checkout

__all__ = ["Lab", "Mirror", "Worktrees", "empty", "lab"]

WRITE = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
OWNED = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR


def _git(repo: Path, *args: str, env: Mapping[str, str] | None = None, timeout: float | None = None) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, timeout=timeout)


@dataclass(frozen=True)
class Lab:
    """The lab remote a head pushed to a grade ref lives on: its URL and the token git sends it, never shown."""

    url: str
    token: str = field(repr=False)

    def environ(self) -> dict[str, str]:
        """Return git's environment for a call to the lab remote, with the token as the basic password of the user
        `lab` for its URL only."""
        secret = base64.b64encode(f"lab:{self.token}".encode()).decode()
        return {
            **os.environ,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{self.url}.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {secret}",
            "GIT_TERMINAL_PROMPT": "0",
        }


def lab(environ: Mapping[str, str]) -> Lab | None:
    """Return the lab remote AGENTIHOOKS_LAB_REMOTE names, holding AGENTIHOOKS_LAB_REMOTE_TOKEN, or None without one."""
    url = environ.get(checkout.URL_ENV)
    return Lab(url, environ.get(checkout.TOKEN_ENV, "")) if url else None


def empty(folder: Path) -> None:
    """Remove every entry of the folder, sealed trees and folders without owner access included, leaving the folder
    itself in place; a symlink's target stays untouched, and a missing folder is not an error. A folder's mode is
    changed only when it lacks an owner bit, so an open folder of another user is emptied as it is."""
    if not folder.is_dir():
        return
    mode = folder.stat().st_mode
    if mode & OWNED != OWNED:
        folder.chmod(mode | OWNED)
    for entry in folder.iterdir():
        if entry.is_symlink():
            entry.unlink()
        elif entry.is_dir():
            empty(entry)
            entry.rmdir()
        else:
            entry.unlink()


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
    """The node's bare mirror, shared and read only, and the pod's private store that borrows its objects, with the
    lab remote it falls back to, if any, and the repository URL its heads belong to when the remote does not name it."""

    node: Path
    store: Path
    remote: str
    lab: Lab | None = None
    repository: str | None = None  # pragma: no mutate
    environ: Mapping[str, str] | None = field(default=None, repr=False)

    def fetch(self, sha: str, timeout: float | None = None) -> bool:
        """Make sha present in the store, fetching only its missing objects from the remote, else from the lab remote
        when it serves the repository, or the remote's repository when the repository is None; True when anything was
        fetched. A caller bounding the network fetch in time passes timeout; every other caller leaves it unbound."""
        if not self.store.exists():
            _git(self.store.parent, "init", "--quiet", "--bare", str(self.store))
            if (self.node / "objects").is_dir():
                (self.store / "objects/info/alternates").write_text(f"{self.node / 'objects'}\n")
        found = subprocess.run(["git", "-C", str(self.store), "cat-file", "-t", sha], capture_output=True, text=True)
        if found.stdout == "commit\n":
            return False
        ref = f"{sha}:refs/lab/{sha}"
        try:
            _git(
                self.store,
                "fetch",
                "--quiet",
                "--no-write-fetch-head",
                self.remote,
                ref,
                env=self.environ,
                timeout=timeout,
            )
        except subprocess.CalledProcessError:
            origin = self.remote if self.repository is None else self.repository
            if self.lab is None or not checkout.serves(self.lab.url, origin):
                raise
            _git(
                self.store,
                "fetch",
                "--quiet",
                "--no-write-fetch-head",
                self.lab.url,
                ref,
                env=self.lab.environ(),
                timeout=timeout,
            )
        return True


@dataclass
class Worktrees:
    """Read only worktrees per head shared by that head's units, evicted least recently leased once idle."""

    mirror: Mirror
    root: Path
    capacity: int
    live: OrderedDict[str, int] = field(default_factory=OrderedDict)

    def lease(self, head: str) -> tuple[Path, bool]:
        """Return the head's shared tree, cutting it on first use, and whether the store fetched the head for it; count
        one more live unit on it."""
        tree = self.root / head
        fetched = False
        if head not in self.live:
            fetched = self.mirror.fetch(head)
            if tree.exists():
                self.drop(tree)
            _git(self.mirror.store, "worktree", "add", "--quiet", str(tree), head)
            _seal(tree)
            self.live[head] = 0
        self.live[head] += 1
        self.live.move_to_end(head)
        self._evict()
        return tree, fetched

    def release(self, head: str) -> None:
        """Count one live unit off the head's tree."""
        self.live[head] -= 1
        self._evict()

    def own(self, head: str, base: str) -> tuple[Path, bool]:
        """Cut a private writable worktree of head for a mutation unit, with base present in the store for the
        comparison the unit makes, and say whether the store fetched head for it."""
        fetched = self.mirror.fetch(head)
        self.mirror.fetch(base)
        self.root.mkdir(parents=True, exist_ok=True)
        tree = Path(tempfile.mkdtemp(prefix=f"{head}.", dir=self.root))
        _git(self.mirror.store, "worktree", "add", "--quiet", str(tree), head)
        return tree, fetched

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
