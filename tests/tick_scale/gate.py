"""Runs whole tick passes on a production sized store and fails when a launch path step outruns its floor."""

import argparse
import json
import sys
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from scripts.swarm import timing
from scripts.swarm.store import RedisStore
from scripts.swarm.tick import tick
from scripts.tick.steps.budgets import SPAWN_NEEDS, floor_of
from tests.swarm.test_tick import FakeRuntime
from tests.tick_scale.seed import SLUG, ScaleLedger, seed_store

GATED = frozenset(SPAWN_NEEDS | {"spawn", "tick view"})
PASSES = 3
PASS_MS = 60_000
OK = "ok"


def miss(record: Mapping[str, Any]) -> str:
    """Return why one step ``record`` breaks its floor, or an empty string when it kept it."""
    name, wall = record["step"], float(record["wall_s"])
    floor = floor_of(name)
    if record.get("abandoned") or record.get("cut"):
        return f"{name} was cut off after {wall:.2f}s against its {floor:g}s floor"
    if record.get("outcome") != OK:
        return f"{name} ended {record.get('outcome')} after {wall:.2f}s"
    if wall > floor:
        return f"{name} took {wall:.2f}s against its {floor:g}s floor"
    return ""


def misses(records: Iterable[Mapping[str, Any]]) -> list[str]:
    """Return every gated step record that broke its floor, then every gated step that never ran."""
    ran = [record for record in records if record.get("step") in GATED and not record.get("rested")]
    found = [reason for record in ran if (reason := miss(record))]
    return found + [f"{name} never ran" for name in sorted(GATED - {record["step"] for record in ran})]


def step_records(store: RedisStore) -> list[dict[str, Any]]:
    """Return every step record the passes wrote to the swarm's steps stream, oldest first."""
    return [json.loads(fields["record"]) for _, fields in store.redis.xrange(store.key(SLUG, "tick:steps"))]


def run_passes(store: RedisStore, now_ms: int) -> None:
    """Run ``PASSES`` recorded tick passes one minute apart over the seeded store and full size ledger."""
    ledger, runtime = ScaleLedger(), FakeRuntime()
    for index in range(PASSES):
        with timing.tick(SLUG, store), timing.pass_record(store, SLUG):
            tick(SLUG, store, ledger, runtime, now_ms=now_ms + index * PASS_MS)


def main(argv: list[str] | None = None) -> int:
    """Seed the store, run the passes, write the report and return 1 when any gated step broke its floor."""
    import redis

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    now_ms = time.time_ns() // 1_000_000
    store = seed_store(redis.Redis.from_url(args.redis, decode_responses=True), now_ms)
    run_passes(store, now_ms)
    records = step_records(store)
    found = misses(records)
    gated = [record for record in records if record.get("step") in GATED]
    args.report.write_text(json.dumps({"gated": gated, "misses": found}, indent=2) + "\n")
    for record in gated:
        print(json.dumps({key: record.get(key) for key in ("step", "outcome", "wall_s", "rested", "abandoned")}))
    for reason in found:
        print(f"::error::{reason}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
