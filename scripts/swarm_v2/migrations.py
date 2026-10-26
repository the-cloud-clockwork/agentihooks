"""Operator command line over the session catalog's staged migrations."""

import argparse
import json
import time
from collections.abc import Callable

from hooks.memory.catalog import Catalog
from hooks.memory.migrations import DIMENSION, OPERATOR, Approval, Migrator
from scripts.gates.base import Who

STAGED = ("backfill", "verify", "activate", "deactivate")
COMMANDS = ("status", "startup", *STAGED, "drop-vectors")
BATCH_SIZE, MAX_BATCHES = 500, 20
REVISION_MISPLACED = "--revision is given with backfill, verify, activate and deactivate and only with them"
SCOPE_MISPLACED = "--domain and --generation are given with drop-vectors and only with it"
AGENT = "agent"


def approval(who: Who, domain: str, generation: str) -> Approval:
    if who.name or who.swarm:
        return Approval(who.name or who.swarm, AGENT, DIMENSION, domain, generation)
    return Approval(OPERATOR, OPERATOR, DIMENSION, domain, generation)


def _run(migrator: Migrator, args: argparse.Namespace, now: float, who: Who | None) -> dict:
    if args.command == "status":
        return {**migrator.status(), "blocked": migrator.dimensions.blocked()}
    if args.command == "startup":
        return migrator.startup()
    if args.command == "backfill":
        return migrator.backfill(args.revision, args.batch_size, args.max_batches)
    if args.command in STAGED:
        return getattr(migrator, args.command)(args.revision)
    caller = Who.from_env() if who is None else who
    return migrator.dimensions.drop(approval(caller, args.domain, args.generation), now)


def main(argv: list[str] | None = None, clock: Callable[[], float] | None = None, who: Who | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("catalog")
    parser.add_argument("--revision", type=int)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--max-batches", type=int, default=MAX_BATCHES)
    parser.add_argument("--domain")
    parser.add_argument("--generation")
    args = parser.parse_args(argv)
    if (args.command in STAGED) != (args.revision is not None):
        parser.error(REVISION_MISPLACED)
    given = [value is not None for value in (args.domain, args.generation)]
    misplaced = not all(given) if args.command == "drop-vectors" else any(given)
    if misplaced:
        parser.error(SCOPE_MISPLACED)
    catalog = Catalog(args.catalog)
    try:
        answer = _run(Migrator(catalog), args, time.time() if clock is None else clock(), who)
    finally:
        catalog.close()
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
