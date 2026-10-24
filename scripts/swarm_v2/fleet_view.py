"""Render the fleet dashboard and its bounded alerts for recorded samples or a recorded scaling canary."""

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from scripts.swarm_v2 import fleet_canary
from scripts.swarm_v2 import fleet_dashboard as fleet


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.swarm_v2.fleet_view", description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--samples", type=Path, help="JSON list of samples in the metrics contract shape")
    source.add_argument("--canary", type=Path, help="recorded SV2-CAP-02 scaling canary result")
    parser.add_argument("--overlay", type=Path, help="quota, archive, checkpoint and brain readings for --canary")
    parser.add_argument("--grant", type=Path, required=True, help="viewer grant naming its brains")
    parser.add_argument("--definition", type=Path, default=fleet.DEFINITION)
    return parser


def traces(args: argparse.Namespace) -> dict[str, list[dict]]:
    if args.samples is not None:
        return {args.samples.stem: json.loads(args.samples.read_text())}
    overlay = json.loads(args.overlay.read_text())
    return fleet_canary.record_samples(json.loads(args.canary.read_text()), overlay)


def render(trace: list[dict], seen: fleet.Viewer, found: dict, environ: Mapping[str, str]) -> dict:
    evaluations = [fleet.evaluate(sample, found, environ) for sample in trace]
    return {
        "dashboard": fleet.dashboard(trace[-1], seen, found),
        "timeline": [
            {"seq": e["seq"], "at_ms": e["at_ms"], "alerts": [f"{a['rule']}:{a['subject']}" for a in e["alerts"]]}
            for e in evaluations
        ],
        "alert_actionability_review": fleet.alert_actionability_review(evaluations[-1], found),
    }


def main(argv: list[str] | None = None, environ: Mapping[str, str] = os.environ) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.canary is not None and args.overlay is None:
        parser.error("--canary needs --overlay")
    try:
        seen = fleet.viewer(json.loads(args.grant.read_text()))
    except fleet.ViewRefused as refused:
        print(f"fleet view refused: {refused}", file=sys.stderr)
        return 2
    found = fleet.definition(args.definition)
    views = {name: render(trace, seen, found, environ) for name, trace in traces(args).items()}
    print(json.dumps(views, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
