"""Per unit sandbox: a private environment, a port range, no network unless asked, the process group killed on timeout."""

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = ["CannotRun", "Outcome", "PortAllocator", "PortRange", "Sandbox", "argv_for", "environment", "whitelist_key"]


class CannotRun(RuntimeError):
    """The unit cannot run on this worker, so its verdict is red."""


@dataclass(frozen=True)
class PortRange:
    first: int
    last: int


@dataclass(frozen=True)
class Outcome:
    code: int | None
    log: Path


class PortAllocator:
    """Hands out disjoint port ranges of one width from a fixed span of ports."""

    def __init__(self, first: int, last: int, width: int) -> None:
        self._free: list[PortRange] = []
        self._lock = threading.Lock()

    def take(self) -> PortRange:
        """Take the lowest free range, or raise CannotRun when every range is taken."""
        raise NotImplementedError

    def give(self, ports: PortRange) -> None:
        """Return a range taken from this allocator."""
        raise NotImplementedError


def whitelist_key(whitelist: Mapping[str, str]) -> str:
    """The sha256 of the canonical JSON of the whitelisted names and values, the sandbox's part of a batch key."""
    raise NotImplementedError


def environment(directory: Path, ports: PortRange, whitelist: Mapping[str, str]) -> dict[str, str]:
    """The whole environment of a unit run in directory: the whitelist under the private variables."""
    raise NotImplementedError


def argv_for(argv: Sequence[str], env: Mapping[str, str], bwrap: str | None) -> list[str]:
    """The command that runs argv with exactly env, inside bubblewrap without network unless bwrap is None."""
    raise NotImplementedError


class Sandbox:
    """Runs units from cwd, each in a private directory under root with a port range and the whitelisted environment."""

    def __init__(self, root: Path, cwd: Path, ports: PortAllocator, whitelist: Mapping[str, str]) -> None:
        self.root = root
        self.cwd = cwd
        self.ports = ports
        self.whitelist = dict(whitelist)

    def run(self, argv: Sequence[str], timeout: float, net: bool = False) -> Outcome:
        """Run argv in its own session; the code is None when timeout ended it and its process group was killed."""
        raise NotImplementedError
