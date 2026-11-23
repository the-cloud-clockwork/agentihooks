"""Measure the processor and wall time of the lab scheduler's load builds against its Redis, reading only.

`python -m scripts.ci_lab.load_measure [--builds N]` connects to the Redis that `CI_LAB_REDIS_URL` names, runs
`Scheduler.load` once per build and prints one JSON object: the build count, the live grade count, each build's
processor and wall seconds, the grade parses the builds ran, and each build's grade documents fetched and demand record
bytes read. A load prunes stale worker offers through the queue's
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

BUILDS = 3
LEAVES_OUT = "the offer prune round trip of each build"

Clock = Callable[[], float]


def prune_nothing(since_ms: int) -> int:
    """Stand in for the queue's offer prune on a measured scheduler: delete no offer and report none dropped."""
    return 0


class Parses:
    """The scheduler's grade parser, counting the parses it runs."""

    def __init__(self, parse: Callable[[str | bytes], scheduler.State]) -> None:
        self.parse = parse
        self.count = 0

    def __call__(self, raw: str | bytes) -> scheduler.State:
        """Count one parse and return the grade it parsed."""
        self.count += 1
        return self.parse(raw)


class Reads:
    """The scheduler's demand record and grade document reads, counting per build the documents fetched and the record
    bytes read."""

    def __init__(
        self,
        records: Callable[[Any, Sequence[str]], list[str | None]],
        document: Callable[[Any, str], str | None],
    ) -> None:
        self.records = records
        self.document = document
        self.documents: list[int] = []
        self.record_bytes: list[int] = []

    def build(self) -> None:
        """Start counting the next build."""
        self.documents.append(0)
        self.record_bytes.append(0)

    def fetch_records(self, client: Any, runs: Sequence[str]) -> list[str | None]:
        """Read the runs' demand records and count their bytes in the current build."""
        found = self.records(client, runs)
        self.record_bytes[-1] += sum(len(record) for record in found if record is not None)
        return found

    def fetch_document(self, client: Any, run: str) -> str | None:
        """Read a run's grade document and count it in the current build when it exists."""
        document = self.document(client, run)
        self.documents[-1] += document is not None
        return document


def measured_scheduler(client: Any) -> scheduler.Scheduler:
    """Return a scheduler over client for load builds: its queue's offer prune deletes nothing, and it holds no
    object store, ports or settings, which a load never reads."""
    engine = scheduler.Scheduler(client, None, None, None)  # pyright: ignore[reportArgumentType]
    engine.queue.prune = prune_nothing
    return engine


def measure(engine: scheduler.Scheduler, builds: int, now_ms: int, process: Clock, wall: Clock) -> dict[str, Any]:
    """Run builds loads at now_ms and return the build count, the live grade count, each build's seconds on the
    process and wall clocks, the grade parses the builds ran, each build's grade documents fetched and demand record
    bytes read, and what the figures leave out."""
    grades = engine.client.scard(scheduler.LIVE)
    parse = scheduler._parse  # pyright: ignore[reportPrivateUsage]
    records = scheduler._records  # pyright: ignore[reportPrivateUsage]
    document = scheduler._document  # pyright: ignore[reportPrivateUsage]
    parses = Parses(parse)
    reads = Reads(records, document)
    spent: list[float] = []
    took: list[float] = []
    scheduler._parse = parses  # pyright: ignore[reportPrivateUsage]
    scheduler._records = reads.fetch_records  # pyright: ignore[reportPrivateUsage]
    scheduler._document = reads.fetch_document  # pyright: ignore[reportPrivateUsage]
    try:
        for _ in range(builds):
            reads.build()
            started, clock = process(), wall()
            engine.load(now_ms)
            spent.append(process() - started)
            took.append(wall() - clock)
    finally:
        scheduler._parse = parse  # pyright: ignore[reportPrivateUsage]
        scheduler._records = records  # pyright: ignore[reportPrivateUsage]
        scheduler._document = document  # pyright: ignore[reportPrivateUsage]
    return {
        "builds": builds,
        "grades": grades,
        "process_seconds": spent,
        "wall_seconds": took,
        "parses": parses.count,
        "documents_fetched": reads.documents,
        "record_bytes_read": reads.record_bytes,
        "leaves_out": LEAVES_OUT,
    }


def main(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    """Measure the scheduler Redis that CI_LAB_REDIS_URL names and print the figures as JSON."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--builds", type=int, default=BUILDS)
    args = parser.parse_args(argv)
    client: Any = redis.Redis.from_url(environ[contract.REDIS_URL], decode_responses=True)  # pyright: ignore[reportUnknownMemberType]
    now_ms = scheduler._now_ms()  # pyright: ignore[reportPrivateUsage]
    print(json.dumps(measure(measured_scheduler(client), args.builds, now_ms, time.process_time, time.perf_counter)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ))
