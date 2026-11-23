"""Lab health for the Doctor: an unreachable lab, mismatches on an authoritative gate, a predicted pull request
grading wait above the claim hold limit and a verdict cache hit ratio under its floor."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any

from scripts.ci_lab import delivery, shadow, shadow_report
from scripts.swarm import claim_hold, lab_capacity, release_wait
from scripts.swarm.health.findings import CI_BEHIND_MINUTES, MINUTE_MS, Finding, ci_behind_minutes

HIT_FLOOR_ENV = "AGENTIHOOKS_DOCTOR_LAB_HIT_FLOOR"
HIT_FLOOR = 0.6
FRACTION = re.compile(r"0(\.\d*)?|1(\.0*)?|\.\d+")
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
        raw = environ.get(HIT_FLOOR_ENV)
        return cls(ci_behind_minutes(environ), float(raw) if raw and FRACTION.fullmatch(raw) else HIT_FLOOR)


def _unreachable(slug: str, block: dict[str, Any], now_ms: int) -> Finding:
    """The failed read as a finding, measured in minutes from the last good read or, with none, the failure."""
    url, error, good = block["url"], block["error"], block.get("at")
    minutes = (now_ms - (good or block["error_at"])) // MINUTE_MS
    return Finding(
        UNREACHABLE,
        slug,
        f"the lab at {url} has not answered a capacity read for {minutes} minutes: {error}",
        (f"lab {url}", f"error at {block['error_at']}: {error}", f"last good read at {good or 'never'}"),
        "the last capacity read failed",
        minutes,
    )


def _behind(slug: str, block: dict[str, Any], now_ms: int, limit: int) -> list[Finding]:
    """The fresh reading's pull request grading wait as a finding when it runs past the limit."""
    minutes = claim_hold.lab_minutes(block, now_ms)
    if minutes <= limit:
        return []
    return [
        Finding(
            BEHIND,
            slug,
            claim_hold.LAB_BEHIND.format(minutes=minutes),
            (f"pull request grading predicted wait {minutes} minutes", f"capacity read at {block['at']}"),
            f"a predicted pull request grading wait above {limit} minutes",
            minutes,
        )
    ]


def _hit_ratios(block: dict[str, Any], now_ms: int, floor: float) -> list[Finding]:
    """One finding per class of a fresh reading whose verdict cache answers under the floor."""
    if now_ms - block["at"] > claim_hold.LAB_FRESH_MS:
        return []
    found: list[Finding] = []
    for name, entry in sorted(block["capacity"]["classes"].items()):
        ratio = entry.get("verdict_cache_hit_ratio")
        if ratio is None or ratio >= floor:
            continue
        found.append(
            Finding(
                HIT_RATIO,
                name,
                f"the lab verdict cache answered {round(ratio * 100)} percent of {name} lookups, "
                f"under the floor of {round(floor * 100)} percent",
                (f"class {name} verdict cache hit ratio {ratio}", f"capacity read at {block['at']}"),
                f"a verdict cache hit ratio below {floor} ({HIT_FLOOR_ENV})",
                round(ratio * 100),
            )
        )
    return found


def findings(slug: str, block: dict[str, Any] | None, now_ms: int, limits: Limits) -> list[Finding]:
    """Every capacity finding of the swarm's stored lab block: a failed last read alone, else a wait past the limit
    and each class under the hit floor."""
    if block is None:
        return []
    if block["error"]:
        return [_unreachable(slug, block, now_ms)]
    return [*_behind(slug, block, now_ms, limits.behind_minutes), *_hit_ratios(block, now_ms, limits.hit_floor)]


def gate_findings(counted: Mapping[str, int], heads: int) -> list[Finding]:
    """One finding per authoritative gate with a mismatch over the newest heads."""
    return [
        Finding(
            MISMATCH,
            gate,
            f"the authoritative gate {gate} disagrees with GitHub Actions on {count} of the newest {heads} "
            "pull request heads",
            (
                f"gate {gate} is authoritative in delivery.yaml",
                f"lab check {shadow_report.LISTED_NAME}{gate} against the github-actions job on the newest {heads} "
                "pull request heads",
            ),
            "any mismatch on an authoritative gate",
            count,
        )
        for gate, count in sorted(counted.items())
        if count > 0
    ]


def _differs(sha: str, runs: Sequence[Mapping[str, Any]], gate: str, sources: shadow.Sources) -> bool:
    """Whether the head holds an Actions verdict and a lab verdict on the listed gate that disagree."""
    found = shadow_report.head(sha, runs, gate, sources, listed=True)
    return found is not None and shadow_report.mismatch(found)


def mismatches(
    fetch: shadow_report.Fetch, repository: str, gates: Sequence[str], sources: shadow.Sources, heads: int
) -> dict[str, int]:
    """Each listed gate's mismatches over the newest pull request heads. The heads and each head's check runs are
    read once for every gate, so a pass costs the same reads whatever the gates; a head counts for a gate when its
    Actions verdict differs from the lab's newest check run under either name the lab posts. No gate, no read."""
    if not gates:
        return {}
    newest = islice(shadow_report.heads_of(fetch, repository, shadow_report.PULL_REQUEST), heads)
    read = [(sha, shadow_report.check_runs(fetch, repository, sha)) for sha in newest]
    return {gate: sum(_differs(sha, runs, gate, sources) for sha, runs in read) for gate in gates}


def read(store: Any, slug: str, now_ms: int, environ: Mapping[str, str]) -> list[Finding]:
    """The capacity findings over the swarm's stored lab reading."""
    return findings(slug, lab_capacity.status(store, slug), now_ms, Limits.from_env(environ))


def read_gates(
    store: Any, slug: str, environ: Mapping[str, str], fetch: shadow_report.Fetch = shadow_report.api
) -> list[Finding]:
    """The mismatch findings over the authoritative gates of the swarm checkout's delivery.yaml, with lab verdicts
    from the App the environment names; no GitHub call while no gate is authoritative."""
    config = store.config(slug)
    root = Path(config.repo)
    gates = delivery.authoritative(root)
    if not gates:
        return []
    app = environ.get(shadow_report.LAB_APP)
    if not app:
        raise ValueError(f"{shadow_report.LAB_APP} names no lab App, so authoritative gate mismatches are unknown")
    sources = shadow.Sources(shadow.static_jobs(root), app)
    return gate_findings(mismatches(fetch, release_wait.repository(config), gates, sources, HEADS), HEADS)
