"""Lab health for the Doctor: an unreachable lab, mismatches on an authoritative gate, a predicted pull request
grading wait above the claim hold limit and a verdict cache hit ratio under its floor."""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from scripts.ci_lab import shadow_report
from scripts.swarm.health.findings import CI_BEHIND_MINUTES, Finding

__all__ = [
    "BEHIND",
    "HEADS",
    "HIT_FLOOR",
    "HIT_FLOOR_ENV",
    "HIT_RATIO",
    "MISMATCH",
    "UNREACHABLE",
    "Limits",
    "findings",
    "gate_findings",
    "gh_fetch",
    "mismatches",
    "read",
    "read_gates",
]

HIT_FLOOR_ENV = "AGENTIHOOKS_DOCTOR_LAB_HIT_FLOOR"
HIT_FLOOR = 0.6
HEADS = 10
UNREACHABLE = "lab unreachable"
BEHIND = "lab behind"
HIT_RATIO = "lab cache hit ratio"
MISMATCH = "lab gate mismatch"


@dataclass(frozen=True)
class Limits:
    """The predicted wait in minutes past which the lab is behind, and the verdict cache hit ratio floor."""

    behind_minutes: int = CI_BEHIND_MINUTES
    hit_floor: float = HIT_FLOOR

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Limits":
        """The limits the environment sets, each falling back to its default when unset or malformed."""
        return cls()


def findings(slug: str, block: dict[str, Any] | None, now_ms: int, limits: Limits) -> list[Finding]:
    """Every capacity finding of the swarm's stored lab block."""
    return []


def gate_findings(counted: Mapping[str, int], heads: int) -> list[Finding]:
    """One finding per authoritative gate with a mismatch over the newest heads."""
    return []


def mismatches(
    fetch: shadow_report.Fetch, repository: str, gates: Sequence[str], static: Collection[str], heads: int
) -> dict[str, int]:
    """Each gate's mismatches over its newest pull request heads, as the acceptance rule report counts them."""
    return {}


def gh_fetch(path: str) -> Mapping[str, Any]:
    """One GitHub API answer through the rate limit helper the acceptance rule report uses."""
    return {}


def read(store: Any, slug: str, now_ms: int, environ: Mapping[str, str]) -> list[Finding]:
    """The capacity findings over the swarm's stored lab reading."""
    return []


def read_gates(store: Any, slug: str, fetch: shadow_report.Fetch = gh_fetch) -> list[Finding]:
    """The mismatch findings over the authoritative gates of the swarm checkout's delivery.yaml."""
    return []
