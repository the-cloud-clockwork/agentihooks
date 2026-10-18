"""Derive conservative worker resource profiles from measured workload runs and gate their promotion."""

import hashlib
import json
import math
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from scripts.swarm_v2.runtime.commands import Principal, Role

CLASSES = ("general", "browser", "compiler", "memory")
IDLE = "idle"
IDLE_REFUSAL = "an idle sample cannot size a worker"
CHILDLESS_REFUSAL = "no child activity"
FAILED_REFUSAL = "a run failed or timed out"
METRICS = (
    "peak_rss_mib",
    "peak_pss_mib",
    "peak_parent_rss_mib",
    "peak_children_rss_mib",
    "peak_cores",
    "peak_parent_cores",
    "peak_children_cores",
    "cpu_seconds",
    "parent_cpu_seconds",
    "children_cpu_seconds",
    "peak_storage_mib",
    "peak_processes",
    "peak_children_processes",
)
INSTALLS = ("promoted", "requalified")
COMPARED = ("peak_rss_mib", "peak_cores", "peak_storage_mib", "peak_processes")
REVIEWER = "requalified_by"


@dataclass(frozen=True)
class Policy:
    margin: float = 0.25
    min_repeats: int = 3
    agent_reserve_mib: int = 1024
    base_storage_mib: int = 2048
    memory_step_mib: int = 256
    cpu_step_millis: int = 250
    storage_step_mib: int = 1024
    tolerance: float = 0.15


class ClaimRefused(Exception):
    def __init__(self, message: str, refused: dict[str, str]) -> None:
        super().__init__(message)
        self.refused = refused


def distribution(values: Sequence[float]) -> dict:
    ordered = sorted(values)

    def rank(share: float) -> float:
        return ordered[max(math.ceil(share * len(ordered)), 1) - 1]

    return {"n": len(ordered), "min": ordered[0], "p50": rank(0.5), "p95": rank(0.95), "max": ordered[-1]}


def round_up(value: float, step: int) -> int:
    return max(math.ceil(value / step), 1) * step


def _refusal(cls: str, runs: list[dict], policy: Policy) -> str | None:
    if cls == IDLE:
        return IDLE_REFUSAL
    if any(r["exit_code"] or r["timed_out"] for r in runs):
        return FAILED_REFUSAL
    if not any(r["children"] > 0 and r["peak_processes"] > 1 for r in runs):
        return CHILDLESS_REFUSAL
    counts: dict[int, int] = {}
    for r in runs:
        counts[r["children"]] = counts.get(r["children"], 0) + 1
    if min(counts.values()) < policy.min_repeats:
        return f"fewer than {policy.min_repeats} repeats at a child count"
    return None


def _grouped(runs: Sequence[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for r in runs:
        grouped.setdefault(r["class"], []).append(r)
    return grouped


def admitted(runs: Sequence[dict], policy: Policy) -> tuple[dict[str, list[dict]], dict[str, str]]:
    accepted, refused = {}, {}
    for cls, group in sorted(_grouped(runs).items()):
        reason = _refusal(cls, group, policy)
        if reason:
            refused[cls] = reason
        else:
            accepted[cls] = group
    return accepted, refused


def profile(runs: Sequence[dict], policy: Policy) -> dict:
    measured = {metric: distribution([r[metric] for r in runs]) for metric in METRICS}
    rss, cores, storage = measured["peak_rss_mib"], measured["peak_cores"], measured["peak_storage_mib"]
    grow = 1 + policy.margin
    return {
        "limits": {
            "memory_mib": round_up((rss["max"] + policy.agent_reserve_mib) * grow, policy.memory_step_mib),
            "cpu_millis": round_up(cores["max"] * 1000 * grow, policy.cpu_step_millis),
            "ephemeral_mib": round_up((storage["max"] + policy.base_storage_mib) * grow, policy.storage_step_mib),
        },
        "requests": {
            "memory_mib": round_up(rss["p95"] + policy.agent_reserve_mib, policy.memory_step_mib),
            "cpu_millis": round_up(cores["p50"] * 1000, policy.cpu_step_millis),
        },
        "measured": measured,
        "uncertainty": {
            "samples": len(runs),
            "children": sorted({r["children"] for r in runs}),
            "relative_spread": (rss["max"] - rss["min"]) / rss["p50"] if rss["p50"] else None,
            "margin": policy.margin,
            "declared": {"agent_reserve_mib": policy.agent_reserve_mib, "base_storage_mib": policy.base_storage_mib},
        },
    }


def coexistence(profiles: dict[str, dict], nodes: Sequence[dict]) -> list[dict]:
    rows = []
    for node in nodes:
        memory = node["allocatable_memory_mib"] - node["colocated_memory_mib"]
        cpu = node["allocatable_cpu_millis"] - node["colocated_cpu_millis"]
        fits = {
            name: max(min(memory // built["limits"]["memory_mib"], cpu // built["requests"]["cpu_millis"]), 0)
            for name, built in sorted(profiles.items())
        }
        together = sum(b["limits"]["memory_mib"] for b in profiles.values()) <= memory and (
            sum(b["requests"]["cpu_millis"] for b in profiles.values()) <= cpu
        )
        rows.append(
            {
                "node": node["name"],
                "free_memory_mib": memory,
                "free_cpu_millis": cpu,
                "fits": fits,
                "all_profiles_together": together,
            }
        )
    return rows


def derive(measured: dict, nodes: Sequence[dict], policy: Policy) -> dict:
    """A report admitting each class whose runs show real child activity; refuses when none qualifies."""
    accepted, refused = admitted(measured["runs"], policy)
    if not accepted:
        raise ClaimRefused("no class was admitted: " + "; ".join(f"{c}: {r}" for c, r in refused.items()), refused)
    built = {cls: profile(runs, policy) for cls, runs in accepted.items()}
    return {
        "suite": measured["suite"],
        "generation": measured["generation"],
        "source": measured["source"],
        "host": measured["host"],
        "tools": measured["tools"],
        "policy": asdict(policy),
        "runs_digest": hashlib.sha256(json.dumps(measured["runs"], sort_keys=True).encode()).hexdigest(),
        "evidence": json.loads(json.dumps({"measurements": measured, "nodes": nodes})),
        "profiles": built,
        "refused": refused,
        "coexistence": coexistence(built, nodes) if nodes else "unmeasured: no node capacity was recorded",
        "worker_process_tree_peak_memory": {
            cls: max(r["peak_rss_mib"] for r in runs) for cls, runs in sorted(_grouped(measured["runs"]).items())
        },
    }


def compare(accepted: dict, candidate: dict, policy: Policy) -> dict:
    material, missing = [], []
    for cls, before in sorted(accepted["profiles"].items()):
        after = candidate["profiles"].get(cls)
        if after is None:
            missing.append(cls)
            continue
        for metric in COMPARED:
            old, new = before["measured"][metric]["max"], after["measured"][metric]["max"]
            if abs(new - old) > policy.tolerance * max(abs(old), 1e-9):
                material.append([cls, metric, old, new])
    tools = sorted(set(accepted["tools"]) | set(candidate["tools"]))
    changed = {
        name: [accepted["tools"].get(name), candidate["tools"].get(name)]
        for name in tools
        if accepted["tools"].get(name) != candidate["tools"].get(name)
    }
    return {"material": material, "missing": missing, "tools_changed": changed}


def _canonical(report: dict) -> str:
    return json.dumps({k: v for k, v in report.items() if k != REVIEWER}, sort_keys=True)


def promote(accepted: dict | None, candidate: dict, policy: Policy, requalified_by: str) -> dict:
    """Decide whether candidate replaces the accepted report.

    A material change, a missing class or a tool change holds it until a named reviewer requalifies it.
    """
    unsized = sorted(set(candidate["profiles"]) - set(CLASSES))
    if unsized:
        return {"decision": "refused", "accepted": accepted, "changes": {"unsized": unsized}}
    if accepted is None:
        return {"decision": "promoted", "accepted": candidate, "changes": None}
    if candidate["generation"] < accepted["generation"]:
        return {"decision": "stale", "accepted": accepted, "changes": None}
    if candidate["generation"] == accepted["generation"]:
        same = _canonical(candidate) == _canonical(accepted)
        return {"decision": "replayed" if same else "conflict", "accepted": accepted, "changes": None}
    changes = compare(accepted, candidate, policy)
    if not (changes["material"] or changes["missing"] or changes["tools_changed"]):
        return {"decision": "promoted", "accepted": candidate, "changes": changes}
    reviewer = requalified_by.strip()
    if reviewer:
        return {
            "decision": "requalified",
            "accepted": {**candidate, REVIEWER: reviewer},
            "changes": {**changes, REVIEWER: reviewer},
        }
    return {"decision": "requalify", "accepted": accepted, "changes": changes}


def _previous(path: Path) -> Path:
    return path.with_name(f"{path.name}.previous")


def _install(path: Path, report: dict) -> None:
    staged = path.with_name(f".{path.name}.{os.getpid()}")
    staged.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    staged.replace(path)


def _rederived(candidate: dict) -> dict | None:
    try:
        evidence = candidate["evidence"]
        report = derive(evidence["measurements"], evidence["nodes"], Policy(**candidate["policy"]))
    except (KeyError, TypeError, ValueError, ClaimRefused):
        return None
    return report if _canonical(report) == _canonical(candidate) else None


def accept(
    path: Path, candidate: dict, policy: Policy, requalified_by: str, *, reviewer: Principal | None = None
) -> dict:
    """Install candidate when promote allows it, keeping the report it replaces for rollback."""
    current = json.loads(path.read_text()) if path.exists() else None
    authorized = (
        isinstance(reviewer, Principal) and reviewer.role is Role.OPERATOR and reviewer.name == requalified_by.strip()
    )
    outcome = promote(current, candidate, policy, requalified_by if authorized else "")
    if outcome["decision"] in INSTALLS:
        verified = _rederived(candidate)
        if verified is None:
            return {
                "decision": "refused",
                "accepted": current,
                "changes": {"evidence": "candidate does not match its recorded runs"},
            }
        outcome["accepted"] = (
            {**verified, REVIEWER: reviewer.name} if outcome["decision"] == "requalified" else verified
        )
        if current is not None:
            _install(_previous(path), current)
        _install(path, outcome["accepted"])
    return outcome


def rollback(path: Path) -> int | None:
    """Swap in the accepted report that the last promotion replaced; None when there is none.

    Each step is a rename, so an interruption leaves both reports on disk.
    """
    previous = _previous(path)
    if not previous.exists():
        return None
    restored = json.loads(previous.read_text())
    held = path.with_name(f".{path.name}.rollback")
    path.replace(held)
    previous.replace(path)
    held.replace(previous)
    return restored["generation"]


def _cell(value: float) -> str:
    return f"{value:g}"


def markdown(report: dict, notes: Sequence[str] = ()) -> str:
    lines = [
        "# Worker resource results",
        "",
        f"Suite `{report['suite']}`, generation {report['generation']}. Source: {report['source'] or 'not recorded'}.",
        "",
        "## Profiles",
        "",
        "| Profile | Memory limit MiB | CPU limit millis | Ephemeral MiB | Memory request MiB | CPU request millis | Runs |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, built in report["profiles"].items():
        limits, requests = built["limits"], built["requests"]
        lines.append(
            f"| {name} | {limits['memory_mib']} | {limits['cpu_millis']} | {limits['ephemeral_mib']} | "
            f"{requests['memory_mib']} | {requests['cpu_millis']} | {built['uncertainty']['samples']} |"
        )
    lines += [
        "",
        "## Measured distributions",
        "",
        "| Profile | Metric | n | min | p50 | p95 | max |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, built in report["profiles"].items():
        for metric, d in built["measured"].items():
            cells = " | ".join(_cell(d[k]) for k in ("min", "p50", "p95", "max"))
            lines.append(f"| {name} | {metric} | {d['n']} | {cells} |")
    lines += ["", "## Refused", ""]
    lines += [f"- {cls}: {reason}" for cls, reason in report["refused"].items()] or ["- none"]
    lines += ["", "## Coexistence", ""]
    if isinstance(report["coexistence"], str):
        lines.append(report["coexistence"])
    else:
        lines += [
            "| Node | Free memory MiB | Free CPU millis | Workers that fit | All profiles at once |",
            "|---|---|---|---|---|",
        ]
        for row in report["coexistence"]:
            fits = ", ".join(f"{name} {count}" for name, count in row["fits"].items())
            lines.append(
                f"| {row['node']} | {row['free_memory_mib']} | {row['free_cpu_millis']} | {fits} | "
                f"{'yes' if row['all_profiles_together'] else 'no'} |"
            )
    lines += ["", "## Measurement context", ""]
    lines += [f"- host {key}: {value}" for key, value in sorted(report["host"].items())]
    lines += [f"- tool {key}: {value}" for key, value in sorted(report["tools"].items())]
    lines += [f"- policy {key}: {value}" for key, value in report["policy"].items()]
    if notes:
        lines += ["", "## Limits of this measurement", "", *(f"- {note}" for note in notes)]
    return "\n".join(lines) + "\n"
