import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

from scripts.swarm_v2.herdr.attach import ClientHome
from scripts.swarm_v2.herdr.endpoints import EndpointRefused
from scripts.swarm_v2.herdr.target import Deliveries, listing


def journal() -> Path:
    return ClientHome(Path.home()).journal


def read(path: Path, now: int) -> dict[str, list[dict]]:
    return listing(Deliveries(path).load(), now)


def main(argv: list[str] | None = None, clock: Callable[[], float] = time.time) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.swarm_v2.herdr.deliveries",
        description="List the pending and settled remote prompts in a pane delivery journal; it reads only.",
    )
    parser.add_argument("--journal", type=Path, help="the journal file, by default this controller's own")
    args = parser.parse_args(argv)
    try:
        shown = read(args.journal or journal(), int(clock()))
    except EndpointRefused as refused:
        print(refused, file=sys.stderr)
        return 1
    print(json.dumps(shown, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
