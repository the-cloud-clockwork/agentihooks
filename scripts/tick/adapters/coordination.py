"""Coordination port adapter: the tick's tools over the swarm store for its config, controller lease, named
locks, launch capacity and swarm marks, each call admitted only while its deadline holds."""

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any, cast

from scripts.swarm import lease as leases
from scripts.tick.deadline import Deadline
from scripts.tick.ports.contracts import STORE_READS, UndeclaredEffect

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

    def _untyped(self) -> Any:
        return self.store

    def _redis(self) -> Any:
        return self._untyped().redis

    def _key(self, *parts: str) -> str:
        key: str = self._untyped().key(self.slug, *parts)
        return key

    def lock(self, name: str, *, deadline: Deadline) -> str | None:
        """Take the named lock until the deadline ends; its token, or None while another holder keeps it."""
        held_ms = max(1, int(deadline.live() * 1000))
        token = uuid.uuid4().hex
        taken = bool(self._redis().set(self._key("lock", name), token, nx=True, px=held_ms))
        return token if taken else None

    def tick_lock(self, held: object, ttl_ms: int, caller: str, *, deadline: Deadline) -> str | None:
        """Take the controller tick lock under the held lease for ``ttl_ms``; its token, or None while another
        live pass holds it."""
        from scripts.swarm import controller

        deadline.live()
        return controller.take_tick_lock(self.store, self.slug, cast(leases.Lease, held), ttl_ms, caller)

    def release_tick_lock(self, token: str, caller: str, *, deadline: Deadline) -> None:
        """Release the controller tick lock when ``token`` still holds it."""
        from scripts.swarm import controller

        deadline.live()
        controller.release_tick_lock(self.store, self.slug, token, caller)

    def configure(self, settings: "SwarmConfig", *, deadline: Deadline) -> "SwarmConfig":
        """Store the settings that differ from the swarm's current config."""
        deadline.live()
        current = self.store.config(self.slug)
        changes = {
            field.name: getattr(settings, field.name)
            for field in fields(settings)
            if getattr(settings, field.name) != getattr(current, field.name)
        }
        if not changes:
            return current
        updated: "SwarmConfig" = self._untyped().update(self.slug, **changes)
        return updated

    def capacity(self, *, deadline: Deadline) -> dict[str, int]:
        from scripts.routing.reservation import LaunchSlots

        deadline.live()
        return LaunchSlots(self._redis()).counts()

    def mark(self, name: str, value: object, *, deadline: Deadline) -> bool:
        """Set the swarm mark name to value, or drop it when value is None."""
        deadline.live()
        key = self._key(name)
        if value is None:
            return bool(self._redis().delete(key))
        return bool(self._redis().set(key, str(value)))

    def marked(self, name: str, *, deadline: Deadline) -> str | None:
        """Return the value of the swarm mark name, or None when it is unset."""
        deadline.live()
        value: str | None = self._redis().get(self._key(name))
        return value

    def _operation(self, operation: str) -> Callable[..., Any]:
        target: Any = self.store
        for part in operation.split("."):
            if part.startswith("_"):
                raise AttributeError(part)
            target = getattr(target, part)
        return target

    def store_read(
        self, operation: str, args: Sequence[object], kwargs: Mapping[str, object], *, deadline: Deadline
    ) -> Any:
        """Answer the named swarm store read, a dotted path such as ``redis.hget``, while the deadline holds."""
        deadline.live()
        if operation not in STORE_READS:
            raise UndeclaredEffect(f"{operation} is a store write")
        return self._operation(operation)(*args, **kwargs)

    def store_write(
        self, operation: str, args: Sequence[object], kwargs: Mapping[str, object], *, deadline: Deadline
    ) -> Any:
        """Apply the named swarm store write, a dotted path such as ``seats.occupy``, while the deadline holds."""
        deadline.live()
        return self._operation(operation)(*args, **kwargs)
