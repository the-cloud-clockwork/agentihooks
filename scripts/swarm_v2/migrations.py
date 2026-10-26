"""Operator command line over the session catalog's staged migrations."""

import argparse
import json
import time
from collections.abc import Callable

from hooks.memory.catalog import Catalog
from hooks.memory.migrations import DIMENSION, Approval, Migrator

STAGED = ("backfill", "verify", "activate", "deactivate")


def _run(migrator: Migrator, args: argparse.Namespace, now: float) -> dict:
    if args.command == "status":
        return {**migrator.status(), "blocked": migrator.dimensions.blocked()}
    if args.command == "startup":
        return migrator.startup()
    if args.command == "backfill":
        return migrator.backfill(args.revision, args.batch_size, args.max_batches)
    if args.command in STAGED:
        return getattr(migrator, args.command)(args.revision)
    approval = Approval(args.actor, args.role, DIMENSION, args.domain, args.generation)
    return migrator.dimensions.drop(approval, now)


def main(argv: list[str] | None = None, clock: Callable[[], float] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("status", "startup", *STAGED, "drop-vectors"))
    parser.add_argument("catalog")
    parser.add_argument("--revision", type=int)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--max-batches", type=int, default=20)
    parser.add_argument("--actor")
    parser.add_argument("--role")
    parser.add_argument("--domain")
    parser.add_argument("--generation")
    args = parser.parse_args(argv)
    if (args.command in STAGED) != (args.revision is not None):
        parser.error("--revision is given with backfill, verify, activate and deactivate and only with them")
    given = [value is not None for value in (args.actor, args.role, args.domain, args.generation)]
    misplaced = not all(given) if args.command == "drop-vectors" else any(given)
    if misplaced:
        parser.error("--actor, --role, --domain and --generation are given with drop-vectors and only with it")
    catalog = Catalog(args.catalog)
    try:
        answer = _run(Migrator(catalog), args, time.time() if clock is None else clock())
    finally:
        catalog.close()
    print(json.dumps(answer, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
