"""The lab verdict store: a write once Redis index over the content addressed report and log objects."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from scripts.ci_lab.objects import Objects

__all__ = ["DISABLED", "INDEX", "PREFIX", "Store", "Verdict"]

PREFIX = "agentihooks-ci:"
INDEX = f"{PREFIX}verdict:"
DISABLED = f"{PREFIX}disabled"


@dataclass(frozen=True)
class Verdict:
    kind: str
    key: str
    passed: bool
    flaky: bool
    seconds: float


class Store:
    """Verdicts by unit key: a write once Redis index over the report and log objects it rebuilds from."""

    def __init__(self, client: Any, objects: Objects) -> None:
        self.client = client
        self.objects = objects

    def get(self, kind: str, key: str, *, bypass_negative: bool = False) -> Verdict | None:
        """Return the stored verdict, or None for an unknown key, a disabled kind or a bypassed negative verdict."""
        raise NotImplementedError

    def put(self, verdict: Verdict, report: Mapping[str, object], log: bytes) -> bool:
        """Store a verdict once; return False and change nothing when its key is already stored."""
        raise NotImplementedError

    def disable(self, kind: str) -> None:
        """Stop reuse of a kind: every get of it misses until enable."""
        raise NotImplementedError

    def enable(self, kind: str) -> None:
        """Resume reuse of a kind."""
        raise NotImplementedError

    def reindex(self) -> int:
        """Rebuild the index from the stored report objects; return how many entries it added."""
        raise NotImplementedError
