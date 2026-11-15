"""Pod git for the lab worker: a private store over the node mirror and read only worktrees per head."""

from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Mirror", "Worktrees"]


@dataclass(frozen=True)
class Mirror:
    """The node's bare mirror, shared and read only, and the pod's private store that borrows its objects."""

    node: Path
    store: Path
    remote: str

    def fetch(self, sha: str) -> bool:
        """Make sha present in the store, fetching only its missing objects; True when anything was fetched."""
        raise NotImplementedError(sha)


@dataclass
class Worktrees:
    """Read only worktrees per head shared by that head's units, evicted least recently leased once idle."""

    mirror: Mirror
    root: Path
    capacity: int
    live: OrderedDict[str, int] = field(default_factory=OrderedDict[str, int])

    def lease(self, head: str) -> Path:
        """Return the head's shared tree, cutting it on first use, and count one more live unit on it."""
        raise NotImplementedError(head)

    def release(self, head: str) -> None:
        """Count one live unit off the head's tree."""
        raise NotImplementedError(head)

    def own(self, head: str) -> Path:
        """Cut a private writable worktree of head for a mutation unit."""
        raise NotImplementedError(head)

    def drop(self, tree: Path) -> None:
        """Remove a worktree and its registration in the store."""
        raise NotImplementedError(tree)
