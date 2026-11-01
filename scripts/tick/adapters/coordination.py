"""Coordination port adapter: the tick's tools over the swarm store for its config, controller lease, named
locks, launch capacity and swarm marks, each call admitted only while its deadline holds."""

import uuid
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING

from scripts.swarm import lease as leases
from scripts.tick.deadline import Deadline

if TYPE_CHECKING:
    from scripts.swarm.store import RedisStore, SwarmConfig

__all__ = ["StoreCoordination"]


@dataclass(frozen=True)
class StoreCoordination:
    """Coordinate one swarm through its store on behalf of the controller named owner."""

    store: "RedisStore"
    slug: str
    owner: str

    def read(self, *, deadline: Deadline) -> "SwarmConfig":
        deadline.live()
        return self.store.config(self.slug)

    def lease(self, *, deadline: Deadline) -> leases.Lease | None:
        deadline.live()
        return leases.acquire(self.store, self.slug, self.owner)

    def lock(self, name: str, *, deadline: Deadline) -> str | None:
        """Take the named lock until the deadline ends; its token, or None while another holder keeps it."""
        held_ms = max(1, int(deadline.live() * 1000))
        token = uuid.uuid4().hex
        taken = self.store.redis.set(self.store.key(self.slug, "lock", name), token, nx=True, px=held_ms)
        return token if taken else None

    def configure(self, settings: "SwarmConfig", *, deadline: Deadline) -> "SwarmConfig":
        """Store the settings that differ from the swarm's current config."""
        deadline.live()
        current = self.store.config(self.slug)
        changes = {
            field.name: getattr(settings, field.name)
            for field in fields(settings)
            if getattr(settings, field.name) != getattr(current, field.name)
        }
        return self.store.update(self.slug, **changes) if changes else current

    def capacity(self, *, deadline: Deadline) -> dict[str, int]:
        from scripts.routing.reservation import LaunchSlots

        deadline.live()
        return LaunchSlots(self.store.redis).counts()

    def mark(self, name: str, value: object, *, deadline: Deadline) -> bool:
        """Set the swarm mark name to value, or drop it when value is None."""
        deadline.live()
        key = self.store.key(self.slug, name)
        if value is None:
            return bool(self.store.redis.delete(key))
        return bool(self.store.redis.set(key, str(value)))
