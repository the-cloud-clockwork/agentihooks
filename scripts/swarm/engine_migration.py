"""One time move of every stored swarm config onto the events engine with a stored repository origin."""

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from scripts.doctor.priming import TEMPLATE, doctor_slug  # pyright: ignore[reportUnknownVariableType]
from scripts.swarm import store
from scripts.swarm.store import EVENTS_ENGINE, RedisStore, SwarmError

BUSY = "skipped: live legacy agents"
NO_ORIGIN = "skipped: no repository origin"


def slugs(redis_store: RedisStore) -> list[str]:
    """Every stored swarm id."""
    return list(map(str, redis_store.slugs()))  # pyright: ignore[reportUnknownArgumentType]


def watched(redis_store: RedisStore, slug: str) -> str:
    """The swarm a Doctor swarm watches, from its peer or its derived name; empty for any other swarm."""
    if redis_store.config(slug).template != TEMPLATE:
        return ""
    peer = str(redis_store.peer(slug))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    return peer or next((s for s in slugs(redis_store) if s != slug and doctor_slug(s) == slug), "")


def own_origin(redis_store: RedisStore, slug: str) -> str:
    """The swarm's stored origin, else the origin of its checkout."""
    from scripts.hive.daemon import origin

    config = redis_store.config(slug)
    return config.repo_origin or origin(Path(config.repo))


def doctor_origin(redis_store: RedisStore, doctor: str, watched_slug: str) -> str:
    """The origin of the stored swarm a Doctor watches, else the Doctor's own origin."""
    stored = watched_slug in slugs(redis_store)
    return (own_origin(redis_store, watched_slug) if stored else "") or own_origin(redis_store, doctor)


def repo_origin(redis_store: RedisStore, slug: str) -> str:
    """The swarm's stored origin; when empty, a Doctor's watched origin or any swarm's own origin."""
    if stored := redis_store.config(slug).repo_origin:
        return stored
    peer = watched(redis_store, slug)
    return doctor_origin(redis_store, slug, peer) if peer else own_origin(redis_store, slug)


def drop_launch_jobs(redis_store: RedisStore, slug: str) -> None:
    """Drops the launch job rows of a swarm with no agents, since every agent they name is gone."""
    from scripts.swarm import launch_job

    redis_store.redis.delete(redis_store.key(slug, launch_job.KEY))  # pyright: ignore[reportUnknownMemberType]


def stored_on_events(redis_store: RedisStore, slug: str) -> bool:
    """Whether the engine stored in the swarm's config hash is events; an absent value is the old legacy default."""
    return str(redis_store.redis.hget(redis_store.key(slug, "config"), "engine")) == EVENTS_ENGINE  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


def drop_stuck_launches(redis_store: RedisStore, slug: str) -> int:
    """Drops the launch rows of a swarm with no agents that still wait for an acknowledgement, keeping its history."""
    return drop_collecting(redis_store.redis, redis_store.key(slug, "launches"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


def drop_collecting(client: Any, key: str) -> int:
    """Deletes the rows of one launches hash still acknowledging or launching and counts them."""
    from scripts.swarm.ack_pass import COLLECTING

    rows: dict[str, str] = client.hgetall(key)
    stuck = [name for name, raw in rows.items() if json.loads(raw).get("state") in COLLECTING]
    if stuck:
        client.hdel(key, *stuck)
    return len(stuck)


def dropped(outcome: str, count: int) -> str:
    """The outcome, naming the stuck launches it dropped when there were any."""
    return f"{outcome}: dropped {count} stuck launches" if count else outcome


def migrate_one(redis_store: RedisStore, slug: str) -> str:
    """Moves one stored config to the events engine with an origin and names the outcome."""
    config = redis_store.config(slug)
    on_events = stored_on_events(redis_store, slug)
    idle = not redis_store.agents(slug)  # pyright: ignore[reportUnknownMemberType]
    if on_events and config.repo_origin:
        return dropped("unchanged", drop_stuck_launches(redis_store, slug) if idle else 0)
    if not idle and not on_events:
        return BUSY
    found = repo_origin(redis_store, slug)
    if not found:
        return NO_ORIGIN
    redis_store.update(slug, engine=EVENTS_ENGINE, repo_origin=found)  # pyright: ignore[reportUnknownMemberType]
    if not on_events:
        drop_launch_jobs(redis_store, slug)
    return dropped(f"migrated: {found}", drop_stuck_launches(redis_store, slug) if idle else 0)


def migrate(redis_store: RedisStore) -> dict[str, str]:
    """Moves every stored config it safely can and reports each swarm's outcome; one refusal never stops the rest."""
    outcomes: dict[str, str] = {}
    for slug in slugs(redis_store):
        try:
            outcomes[slug] = migrate_one(redis_store, slug)
        except SwarmError as exc:
            outcomes[slug] = f"skipped: {exc}"
    return outcomes


def start_on_events(redis_store: RedisStore, doctor: str, watched_slug: str) -> None:
    """Puts a new Doctor swarm on the events engine with the watched origin, else its own, when either is known."""
    if found := doctor_origin(redis_store, doctor, watched_slug):
        redis_store.update(doctor, engine=EVENTS_ENGINE, repo_origin=found)  # pyright: ignore[reportUnknownMemberType]


def main(argv: Sequence[str] | None = None) -> None:
    """Migrates every stored swarm config and prints each outcome; a second run changes nothing."""
    argparse.ArgumentParser().parse_args(argv)
    print(json.dumps(migrate(store.connect())))  # pyright: ignore[reportUnknownMemberType]


if __name__ == "__main__":
    main()
