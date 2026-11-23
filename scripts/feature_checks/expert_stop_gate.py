"""Live check of the expert Stop gate: every swarm that is not stopped keeps the gate at enforce, so the stop of an
expert whose lineage still runs is refused there. It prints one line per swarm and never an account or a
credential."""

import sys
from collections.abc import Iterable
from typing import Any, Protocol

from scripts.gates import modes
from scripts.gates.expert_stop import ExpertStop

FAIL = "FAIL: "
HELD = "{slug} keeps the expert stop gate at enforce"
LOOSE = "{slug} sets the expert stop gate to {mode}, so an expert of a running lineage may stop"
NONE = "no swarm is running"


class Fleet(Protocol):
    """What the check reads of the swarm store: the swarms and each one's state and gate modes."""

    def slugs(self) -> Iterable[str]: ...

    def config(self, slug: str) -> Any: ...


def judged(slug: str, gates: dict[str, str]) -> str:
    """The line for one swarm: the gate held at enforce, or failing with the mode the swarm set."""
    mode: str = modes.configured(ExpertStop(), gates)  # pyright: ignore[reportUnknownMemberType]
    if mode == ExpertStop.default_mode:
        return HELD.format(slug=slug)
    return FAIL + LOOSE.format(slug=slug, mode=mode)


def lines(store: Fleet) -> list[str]:
    """One line per swarm that is not stopped, in swarm order, or one line that says none runs."""
    found = [
        judged(slug, config.gates)
        for slug in sorted(store.slugs())
        if (config := store.config(slug)).state != "stopped"
    ]
    return found or [NONE]


def main(store: Fleet | None = None) -> int:
    if store is None:
        from scripts.swarm.store import RedisStore, redis_client  # pyright: ignore[reportUnknownVariableType]

        store = RedisStore(redis_client())
    found = lines(store)
    for line in found:
        print(line)
    return int(any(line.startswith(FAIL) for line in found))


if __name__ == "__main__":
    sys.exit(main())
