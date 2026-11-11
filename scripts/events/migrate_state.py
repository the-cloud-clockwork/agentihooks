"""Inventory or migrate one swarm's legacy key value state bucket into its scoped state stream."""

import argparse
import json
from dataclasses import replace
from typing import Any

from nats.errors import Error as NatsError
from nats.js.errors import APIError, NoKeysError, NotFoundError

from .config import BusConfig
from .nats import DEFAULT_URL, NatsBus, NatsStateStore
from .nats_migration import IMPORT_PLAN
from .nats_state import IMPORT_PENDING
from .resource_names import legacy_stream_name
from .subjects import stream_name, subject


def _target(bus: Any, swarm: str, bucket: str, keys: list[str]) -> tuple[int, bool]:
    """Owned revisions the swarm's scoped state stream holds and whether a completed import marks it."""
    prefix = subject(swarm, "state", bucket)
    for name in (legacy_stream_name(swarm, f"STATE_{bucket}"), stream_name(swarm, f"STATE_{bucket}")):
        try:
            info = bus._loop.run(bus._js.stream_info(name, subjects_filter=f"{prefix}.>"))
        except NotFoundError:
            continue
        metadata: dict[str, str] = info.config.metadata or {}
        held: dict[str, int] = info.state.subjects or {}
        copied = sum(held.get(subject(swarm, "state", bucket, *key.split(".")), 0) for key in keys)
        return copied, IMPORT_PLAN in metadata and IMPORT_PENDING not in metadata
    return 0, False


def _legacy(bus: Any, bucket: str, keys: list[str]) -> tuple[list[str], int]:
    """Every key the legacy bucket holds and the retained revisions of the owned keys."""
    try:
        kv = bus._loop.run(bus._js.key_value(bucket))
    except NotFoundError:
        return [], 0
    store = NatsStateStore(bus._loop, bus._js, bucket, False)
    try:
        present: list[str] = sorted(bus._loop.run(kv.keys()))
    except NoKeysError:
        present = []
    return present, sum(1 for key in keys for _ in store.watch(key))


def _parser() -> argparse.ArgumentParser:
    """The command line of the state migration."""
    parser = argparse.ArgumentParser(
        description="Inventory or migrate one swarm's legacy state bucket to its scoped state stream"
    )
    parser.add_argument("--swarm", required=True, help="The swarm that owns the migrated keys")
    parser.add_argument("--bucket", required=True, help="The legacy key value bucket to migrate")
    owners = parser.add_mutually_exclusive_group()
    owners.add_argument("--key", action="append", default=[], help="A legacy key this swarm owns; repeat for more")
    owners.add_argument("--no-keys", action="store_true", help="Claim that this swarm owns no legacy key")
    parser.add_argument("--apply", action="store_true", help="Move the owned keys; default is a read only dry run")
    parser.add_argument("--quiescent", action="store_true", help="Confirm legacy writers of the bucket are quiet")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Print what the migration would move, or move it with --apply, as one JSON report."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.apply and (not args.quiescent or not (args.key or args.no_keys)):
        parser.error("--apply requires explicit --key or --no-keys ownership and --quiescent")
    config = BusConfig.from_environ()
    if config.backend != "nats" or args.apply and not config.provision:
        parser.error("migration requires the NATS backend and privileged provisioning for --apply")
    keys = sorted(set(args.key))
    bus: Any = NatsBus(replace(config, scoped=True, provision=args.apply), config.url or DEFAULT_URL)
    try:
        present, retained = _legacy(bus, args.bucket, keys)
        before, imported = _target(bus, args.swarm, args.bucket, keys)
        failures: list[str] = []
        moved = 0
        if args.apply:
            try:
                bus.migrate_state(args.swarm, args.bucket, keys)
            except (RuntimeError, ValueError, PermissionError, APIError, NatsError) as error:
                failures.append(str(error))
            moved = _target(bus, args.swarm, args.bucket, keys)[0] - before
        print(
            json.dumps(
                {
                    "applied": args.apply,
                    "swarm": args.swarm,
                    "bucket": args.bucket,
                    "keys": keys,
                    "unowned": [key for key in present if key not in keys],
                    "retained": retained,
                    "imported": imported,
                    "would_move": 0 if imported else retained - before,
                    "moved": moved,
                    "failures": failures,
                }
            )
        )
        return 1 if failures else 0
    finally:
        bus.close()


if __name__ == "__main__":
    raise SystemExit(main())
