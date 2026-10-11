"""Reduce a read of cluster and autoscaling state to startup samples and report their stages."""

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from scripts.swarm_v2 import startup_stages
from scripts.swarm_v2.resource_profiles import ClaimRefused

LISTINGS = ("nodes", "events", "pods", "activities", "instances")
STAGE_NAMES = {
    "trigger": "Unschedulable to scale up",
    "instance": "Scale up to EC2 launch",
    "join": "EC2 launch to node Ready",
    "schedule": "Node Ready or Pod creation to Scheduled",
    "image": "Image pull",
    "pull_retry": "Failed pulls before the pull that succeeded",
    "pod_start": "Scheduled to last container start, less pulls and retries",
    "bootstrap": "Last container start to Pod Ready",
}


def read(capture: Path) -> dict:
    """The capture folder holds read_at and one JSON listing per name in LISTINGS."""
    raw = {name: json.loads((capture / f"{name}.json").read_text(encoding="utf-8")) for name in LISTINGS}
    raw["read_at"] = (capture / "read_at").read_text(encoding="utf-8").strip()
    reduced = startup_stages.reduce(raw)
    reduced["inputs"] = {name: hashlib.sha256((capture / f"{name}.json").read_bytes()).hexdigest() for name in LISTINGS}
    return reduced


def _cell(spread: dict | None) -> str:
    if not spread:
        return "n/a"
    return f"{spread['p50'] / 1000:.1f} / {spread['p95'] / 1000:.1f} (n {spread['n']})"


def _counts(counts: dict) -> str:
    return ", ".join(f"{kind} {count}" for kind, count in counts.items()) or "none"


def markdown(result: dict, source: str) -> str:
    lines = [
        "# Swarm v2 startup stages",
        "",
        f"Source: {source}. Cells are p50 / p95 in seconds with the sample count.",
        "",
        "| Scenario | Samples | Started | Failures | Transient failures | Node provision to schedulable | "
        "Image pull with retries | " + " | ".join(STAGE_NAMES.values()) + " |",
        "|" + "---|" * (7 + len(STAGE_NAMES)),
    ]
    for name, row in result["scenarios"].items():
        stages = " | ".join(_cell(row["stages"].get(stage)) for stage in STAGE_NAMES)
        lines.append(
            f"| {name} | {row['samples']} | {row['started']} | {_counts(row['failures'])} | "
            f"{_counts(row['transient'])} | {_cell(row['node_provision_ms'])} | {_cell(row['image_init_ms'])} | "
            f"{stages} |"
        )
    return "\n".join(lines) + "\n"


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="startup_bench")
    commands = parser.add_subparsers(dest="command", required=True)
    reduce = commands.add_parser("reduce")
    reduce.add_argument("--capture", type=Path, required=True)
    reduce.add_argument("--output", type=Path, required=True)
    report = commands.add_parser("report")
    report.add_argument("--read", type=Path, required=True)
    report.add_argument("--source", required=True)
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--markdown", type=Path)
    claim = commands.add_parser("claim")
    claim.add_argument("--read", type=Path, required=True)
    claim.add_argument("--scenario", required=True)
    claim.add_argument("--label", default=startup_stages.COLD_LABEL)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "reduce":
        _write(args.output, read(args.capture))
        return 0
    samples = startup_stages.attribute(json.loads(args.read.read_text(encoding="utf-8")))
    if args.command == "report":
        result = {**startup_stages.report(samples), "source": args.source}
        _write(args.output, {**result, "samples": samples})
        if args.markdown:
            args.markdown.write_text(markdown(result, args.source), encoding="utf-8")
        return 0
    chosen = [entry for entry in samples if startup_stages.scenario(entry) == args.scenario]
    try:
        startup_stages.claim(chosen, args.label)
    except ClaimRefused as refusal:
        print(json.dumps({"refused": str(refusal), "samples": refusal.refused}, sort_keys=True))
        return 2
    print(json.dumps({"claimed": args.label, "samples": len(chosen)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
