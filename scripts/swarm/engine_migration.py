"""One time move of every stored swarm config onto the events engine with a stored repository origin."""

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from scripts.doctor.priming import TEMPLATE, doctor_slug  # pyright: ignore[reportUnknownVariableType]
from scripts.swarm import store
from scripts.swarm.store import EVENTS_ENGINE, RedisStore


def watched(redis_store: RedisStore, slug: str) -> str:
    """The swarm a Doctor swarm watches, from its peer or its derived name; empty for any other swarm."""
    if redis_store.config(slug).template != TEMPLATE:
        return ""
    peer = cast("str", redis_store.peer(slug))  # pyright: ignore[reportUnknownMemberType]
    return peer or next((s for s in slugs(redis_store) if s != slug and doctor_slug(s) == slug), "")


def slugs(redis_store: RedisStore) -> list[str]:
    """Every stored swarm id."""
    return cast("list[str]", redis_store.slugs())


def own_origin(redis_store: RedisStore, slug: str) -> str:
    """The swarm's stored origin, else the origin of its checkout."""
    from scripts.hive.daemon import origin

    config = redis_store.config(slug)
    return config.repo_origin or origin(Path(config.repo))


def repo_origin(redis_store: RedisStore, slug: str) -> str:
    """The origin of the swarm a Doctor watches, else the swarm's own origin."""
    peer = watched(redis_store, slug)
    return (own_origin(redis_store, peer) if peer else "") or own_origin(redis_store, slug)


def migrate(redis_store: RedisStore) -> dict[str, str]:
    """Moves every stored config to the events engine with an origin and reports each swarm's outcome."""
    outcomes: dict[str, str] = {}
    for slug in slugs(redis_store):
        config = redis_store.config(slug)
        found = repo_origin(redis_store, slug)
        if config.engine == EVENTS_ENGINE and config.repo_origin:
            outcomes[slug] = "unchanged"
        elif found:
            redis_store.update(slug, engine=EVENTS_ENGINE, repo_origin=found)  # pyright: ignore[reportUnknownMemberType]
            outcomes[slug] = f"migrated: {found}"
        else:
            outcomes[slug] = "skipped: no repository origin"
    return outcomes


def start_on_events(redis_store: RedisStore, doctor: str, watched_slug: str) -> None:
    """Puts a new Doctor swarm on the events engine with the origin of the swarm it watches, when that is known."""
    if found := own_origin(redis_store, watched_slug):
        redis_store.update(doctor, engine=EVENTS_ENGINE, repo_origin=found)  # pyright: ignore[reportUnknownMemberType]


def main(argv: Sequence[str] | None = None) -> None:
    """Migrates every stored swarm config and prints each outcome; a second run changes nothing."""
    argparse.ArgumentParser().parse_args(argv)
    print(json.dumps(migrate(store.connect()), indent=1))  # pyright: ignore[reportUnknownMemberType]


if __name__ == "__main__":
    main()
