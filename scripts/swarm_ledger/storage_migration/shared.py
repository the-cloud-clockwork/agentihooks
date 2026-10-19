import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from scripts.swarm_ledger import storage
from scripts.swarm_ledger.repository import LedgerRepository, postgres
from scripts.swarm_ledger.storage_migration import hosted

DESTINATION = "shared database"
COUNTED = ("tasks", "events", "comments", "artifacts", "registries")


class DryRun(Exception):
    pass


def keep() -> None:
    pass


def rollback() -> None:
    raise DryRun


def comments(value: object) -> int:
    if isinstance(value, list):
        return sum(comments(item) for item in value)
    if not isinstance(value, dict):
        return 0
    own = value.get("comments")
    return (len(own) if isinstance(own, list) else 0) + sum(comments(item) for item in value.values())


def counts(state: dict, registries: list) -> dict:
    return {
        "tasks": len(state.get("tasks", [])),
        "events": len(state["_meta"].get("events", [])),
        "comments": comments({key: value for key, value in state.items() if key != "_meta"}),
        "artifacts": len(state.get("artifacts", [])),
        "registries": len(registries),
    }


def slugs(repo: LedgerRepository) -> list:
    with repo.snapshot() as snapshot:
        return snapshot.slugs() if snapshot else []


def copy(
    source: LedgerRepository, target: LedgerRepository, slug: str, finish: Callable[[], None], replace: bool = False
) -> dict:
    with source.snapshot() as snapshot:
        state, registries, token = snapshot.export(slug), snapshot.registries(slug), snapshot.token(slug)
    try:
        with target.transaction() as transaction:
            exists = transaction.exists(slug)
            stale = not exists or transaction.export(slug) != state
            moved = transaction.registries(slug) != registries
            if exists and (stale or moved) and not replace:
                raise ValueError(f"ledger {slug} differs in the shared database; conversion refuses to replace it")
            if stale:
                stored, seeds = hosted.unseeded(state)
                transaction.insert(slug, stored, seeds, token)
            if moved:
                transaction.replace_registries(slug, registries)
            converted, kept = transaction.export(slug), transaction.registries(slug)
            finish()
    except DryRun:
        pass
    source_counts, shared_counts = counts(state, registries), counts(converted, kept)
    match = source_counts == shared_counts and converted == state and kept == registries
    return {"slug": slug, "source": source_counts, "shared": shared_counts, "match": match}


def freeze(source: LedgerRepository) -> None:
    hosted.fence(source, "*", DESTINATION)


def thaw(source: LedgerRepository, target: LedgerRepository) -> list:
    frozen = {}
    with source.snapshot() as snapshot:
        for slug in snapshot.slugs():
            frozen[slug] = snapshot.export(slug), snapshot.registries(slug)
    with target.transaction() as transaction:
        present = [slug for slug in frozen if transaction.exists(slug)]
        for slug in present:
            if (transaction.export(slug), transaction.registries(slug)) != frozen[slug]:
                raise ValueError(
                    f"ledger {slug} changed in the shared database; unfencing would leave two writable stores"
                )
        for slug in present:
            transaction.purge(slug)
    with source.transaction() as transaction:
        transaction.unfence(DESTINATION)
    return present


def convert(source: LedgerRepository, target: LedgerRepository, dry_run: bool = False) -> dict:
    copied = set()
    if not dry_run:
        for slug in slugs(source):
            copy(source, target, slug, keep)
            copied.add(slug)
        freeze(source)
    finish = rollback if dry_run else keep
    ledgers = [copy(source, target, slug, finish, slug in copied) for slug in slugs(source)]
    totals = {side: {key: sum(row[side][key] for row in ledgers) for key in COUNTED} for side in ("source", "shared")}
    return {
        "ledgers": ledgers,
        "totals": {"ledgers": len(ledgers), **totals},
        "match": all(row["match"] for row in ledgers),
        "read_only": storage.folder_frozen(source.directory),
        "dry_run": dry_run,
    }


def main() -> None:
    from scripts.swarm_ledger import ports

    ports.hooks().config  # noqa: B018

    parser = argparse.ArgumentParser(prog="python -m scripts.swarm_ledger.storage_migration.shared")
    parser.add_argument("--source", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--unfence", action="store_true")
    args = parser.parse_args()
    if not (args.source / storage.DATABASE).is_file():
        sys.exit(f"shared conversion: {args.source} holds no ledger database")
    if not storage.shared():
        sys.exit(f"shared conversion: {postgres.URL} names no shared database")
    source = storage.build(args.source / storage.DATABASE)
    target = storage.build()
    if args.unfence:
        removed = thaw(source, target)
        print(json.dumps({"removed": removed, "read_only": storage.folder_frozen(source.directory)}))
        return
    print(json.dumps(convert(source, target, args.dry_run)))


if __name__ == "__main__":
    main()
