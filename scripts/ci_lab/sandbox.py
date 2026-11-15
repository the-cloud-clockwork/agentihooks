"""Per unit sandbox: private environment, port range, no network unless asked, process group killed on timeout.

The private HOME, TMPDIR and XDG folders are environment variables; the host filesystem stays shared.
"""

import hashlib
import json
import shutil
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from scripts.ci_mutation.runner import run_process

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
        starts = range(first, last - width + 2, width)
        self._free = [PortRange(first=start, last=start + width - 1) for start in starts]
        self._lock = threading.Lock()

    def take(self) -> PortRange:
        """Take the next free range, or raise CannotRun when every range is taken."""
        with self._lock:
            if not self._free:
                raise CannotRun("no free port range")
            return self._free.pop(0)

    def give(self, ports: PortRange) -> None:
        """Return a range taken from this allocator."""
        with self._lock:
            self._free.append(ports)


def whitelist_key(whitelist: Mapping[str, str]) -> str:
    """The sha256 of the canonical JSON of the whitelisted names and values, the sandbox's part of a batch key."""
    text = json.dumps(dict(whitelist), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def environment(directory: Path, ports: PortRange, whitelist: Mapping[str, str]) -> dict[str, str]:
    """The whole environment of a unit run in directory: the whitelist under the private variables."""
    home = directory / "home"
    return {
        **whitelist,
        "HOME": str(home),
        "TMPDIR": str(directory / "tmp"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_RUNTIME_DIR": str(directory / "runtime"),
        "TZ": "UTC",
        "LANG": "C.UTF-8",
        "CI": "true",
        "PYTHONDONTWRITEBYTECODE": "1",
        "CI_LAB_PORT_FIRST": str(ports.first),
        "CI_LAB_PORT_LAST": str(ports.last),
    }


def argv_for(argv: Sequence[str], env: Mapping[str, str], bwrap: str | None) -> list[str]:
    """The command that runs argv with exactly env, inside bubblewrap without network unless bwrap is None."""
    isolation = [] if bwrap is None else [bwrap, "--dev-bind", "/", "/", "--unshare-net", "--die-with-parent", "--"]
    return ["env", "-i", *(f"{name}={value}" for name, value in sorted(env.items())), *isolation, *argv]


def _bwrap() -> str:
    """The bubblewrap executable, or CannotRun when it is not installed."""
    path = shutil.which("bwrap")
    if path is None:
        raise CannotRun("bwrap is not installed, so a unit without network cannot run")
    return path


class Sandbox:
    """Runs units from cwd, each in a private directory under root with a port range and the whitelisted environment."""

    def __init__(
        self, root: Path, cwd: Path, ports: PortAllocator, whitelist: Mapping[str, str], readonly: Sequence[Path] = ()
    ) -> None:
        self.root = root
        self.cwd = cwd
        self.ports = ports
        self.whitelist = dict(whitelist)
        self.readonly = tuple(readonly)

    def run(self, argv: Sequence[str], timeout: float, net: bool = False) -> Outcome:
        """Run argv in its own session; the code is None when timeout ended it and its process group was killed.

        The run directory, the log's parent, keeps the private folders for the caller to remove.
        """
        bwrap = None if net else _bwrap()
        ports = self.ports.take()
        try:
            directory = Path(tempfile.mkdtemp(dir=self.root))
            env = environment(directory, ports, self.whitelist)
            for name in ("HOME", "TMPDIR", "XDG_RUNTIME_DIR"):
                Path(env[name]).mkdir(mode=0o700)
            log = directory / "log.txt"
            return Outcome(code=run_process(argv_for(argv, env, bwrap), self.cwd, timeout, log), log=log)
        finally:
            self.ports.give(ports)
