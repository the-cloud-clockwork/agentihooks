"""Measure the processor and wall time of the lab scheduler's load builds against its Redis, reading only.

`python -m scripts.ci_lab.load_measure [--builds N]` connects to the Redis that `CI_LAB_REDIS_URL` names, runs
`Scheduler.load` once per build and prints one JSON object: the build count, the live grade count, each build's
processor and wall seconds and the grade parses the builds ran. A load prunes stale worker offers through the queue's
prune script, its one write, so the measured scheduler's queue keeps every offer instead; the offers read keeps only
offers made inside the ready window, so each build returns the load the scheduler builds. The figures leave out that
one prune round trip per build.
"""

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import redis

from scripts.ci_lab import contract, scheduler
from scripts.ci_lab.queue import Queue

BUILDS = 3
LEAVES_OUT = "the offer prune round trip of each build"

Clock = Callable[[], float]


class KeptOffers(Queue):
    """The scheduler's queue with its offer prune turned into a no op, so a measured build deletes no offer."""


class Parses:
    """The scheduler's grade parser, counting the parses it runs."""

    def __init__(self, parse: Callable[[str | bytes], scheduler.State]) -> None:
        self.parse = parse
        self.count = 0

    def __call__(self, raw: str | bytes) -> scheduler.State:
        """Count one parse and return the grade it parsed."""
        return self.parse(raw)


def reader(client: Any) -> scheduler.Scheduler:
    """Return a scheduler over client for load builds: its queue keeps every offer, and it holds no object store,
    ports or settings, which a load never reads."""
    engine = scheduler.Scheduler(client, None, None, None)  # pyright: ignore[reportArgumentType]
    engine.queue = KeptOffers(client)
    return engine


def measure(engine: scheduler.Scheduler, builds: int, now_ms: int, process: Clock, wall: Clock) -> dict[str, Any]:
    """Run builds loads at now_ms and return the build count, the live grade count, each build's seconds on the
    process and wall clocks, the grade parses the builds ran and what the figures leave out."""
    parses = Parses(getattr(scheduler, "_parse"))
    for _ in range(builds):
        engine.load(now_ms)
    return {
        "builds": builds,
        "grades": int(engine.client.scard(scheduler.LIVE)),
        "process_seconds": [process() * 0],
        "wall_seconds": [wall() * 0],
        "parses": parses.count,
        "leaves_out": LEAVES_OUT,
    }


def main(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    """Measure the scheduler Redis that CI_LAB_REDIS_URL names and print the figures as JSON."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--builds", type=int, default=BUILDS)
    args = parser.parse_args(argv)
    client: Any = redis.Redis.from_url(environ[contract.REDIS_URL], decode_responses=True)  # pyright: ignore[reportUnknownMemberType]
    now_ms = scheduler._now_ms()  # pyright: ignore[reportPrivateUsage]
    print(json.dumps(measure(reader(client), args.builds, now_ms, time.process_time, time.perf_counter)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ))
