"""agentihooks seat brief: print the caller's seat handoff, culture, latest recap and learned notes, in that order."""

import argparse
import os
import sys
from collections.abc import Mapping

from scripts.seat_brief import sources
from scripts.swarm.store import SwarmError, store_errors

NO_SEAT = "seat brief: this session holds no swarm or quick swarm seat, so there are no seat notes to read."
NONE = "none"
LABELS = ("Handoff", "Culture", "Latest recap", "Learned notes")


def build_parser() -> argparse.ArgumentParser:
    """The agentihooks seat command line; brief is its one command and reads only."""
    parser = argparse.ArgumentParser(prog="agentihooks seat", description="Read the seat this session holds.")
    commands = parser.add_subparsers(required=True)
    commands.add_parser("brief", help="Print the seat's handoff, culture, latest recap and learned notes, in order")
    return parser


def render(brief: sources.Brief) -> str:
    """The brief under plain labels in reading order, a missing part shown as none."""
    lines = [f"Seat {brief.seat}."]
    for label, text in zip(LABELS, (brief.handoff, brief.culture, brief.recap, brief.learned)):
        lines += [f"{label}:", text.strip() or NONE]
    return "\n".join(lines)


def main(argv: list[str], environ: Mapping[str, str] | None = None) -> int:
    """Print the brief of the seat this session holds, or the no seat line; 1 when the seat's store fails."""
    build_parser().parse_args(argv)
    try:
        source = sources.find(os.environ if environ is None else environ)
        brief = source.brief() if source else None
    except (SwarmError, OSError, *store_errors()) as exc:
        print(f"seat brief: {exc}", file=sys.stderr)
        return 1
    print(render(brief) if brief else NO_SEAT)
    return 0
