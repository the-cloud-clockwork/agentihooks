"""Hub relay for hive exit reports: a hive reports on its own subject and the hub publishes only exits of its own executions."""

import functools
import json
from collections.abc import Callable
from typing import Any

from scripts.events.envelope import Envelope
from scripts.events.subjects import EXITS, subject
from scripts.swarm.store import SwarmError, connect  # pyright: ignore[reportUnknownVariableType]

QUEUE = "hub-exits"


def _relay(bus: Any, store: Any, reported: str, data: bytes) -> bool:
    parts = reported.split(".")
    if len(parts) != 3 or parts[0] != EXITS:
        raise ValueError(f"not an exit report subject: {reported!r}")
    swarm, hive = parts[1], parts[2]
    event = Envelope.decode(data)
    if (event.type, event.entity, event.swarm) != ("execution.exited", "execution", swarm):
        raise ValueError(f"exit report on {reported!r} carries {event.type!r} for swarm {event.swarm!r}")
    try:
        agent = store.execution_registry.get(swarm, event.key)
    except SwarmError:
        return False
    if agent.hive != hive:
        raise PermissionError(
            f"hive {hive!r} reported an exit for execution {event.key!r}, which runs on hive {agent.hive!r}"
        )
    return bus.for_swarm(swarm).events.publish(subject(swarm, "execution", "exited"), event)


def handle(bus: Any, store: Callable[[], Any], reported: str, data: bytes) -> bytes:
    """Answer one exit report, publishing it on the shared exited subject only for the reporting hive's execution."""
    try:
        answer = {"ok": True, "result": _relay(bus, store(), reported, data)}
    except Exception as exc:
        answer = {"ok": False, "error": str(exc) or type(exc).__name__}
    return json.dumps(answer).encode()


def serve(bus: Any, store: Callable[[], Any] = connect) -> None:
    """Serve every hive's exit report subject in one hub queue group, opening the swarm store once."""
    bus.serve(f"{EXITS}.*.*", QUEUE, functools.partial(handle, bus, functools.cache(store)))
