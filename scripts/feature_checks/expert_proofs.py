"""Live check of the mission proofs: in every swarm that is not stopped, each lineage that reads complete holds the
mission proof it ended on, and that proof conforms to the mission proof contract against its saved mission. It prints
one line per completed lineage and never an account or a credential."""

import sys
from collections.abc import Iterable
from typing import Any, Protocol

from scripts.handoff import expert
from scripts.swarm import expert_lineage
from scripts.swarm.store import SwarmError

FAIL = "FAIL: "
PROVEN = "{slug} lineage {id} ended on a mission proof that conforms"
BARE = "{slug} lineage {id} reads complete and holds no mission proof"
NONE = "no expert lineage has completed"


class Proofs(Protocol):
    """What the check reads of the swarm store: the swarms, each one's state and its keys."""

    redis: Any

    def slugs(self) -> Iterable[str]: ...

    def config(self, slug: str) -> Any: ...

    def key(self, slug: str, *parts: str) -> str: ...


def judged(store: Proofs, slug: str, lineage: expert_lineage.Lineage) -> str:
    """The line for one completed lineage: conforming, or failing with every fault of its stored proof."""
    text = expert_lineage.proof(store, slug, lineage.id)
    if not text:
        return FAIL + BARE.format(slug=slug, id=lineage.id)
    try:
        found = expert.proof_problems(text, _taken, _taken, slug, expert_lineage.mission(store, slug, lineage))
    except SwarmError as fault:
        found = [str(fault)]
    if found:
        return f"{FAIL}{slug} lineage {lineage.id}: {'; '.join(found)}"
    return PROVEN.format(slug=slug, id=lineage.id)


def _taken(reference: str) -> bool:
    """An address resolved and a pull request was merged when the command took the proof; neither is asked again."""
    return True


def lines(store: Proofs) -> list[str]:
    """One line per completed lineage of the swarms that are not stopped, or one line that says none completed."""
    found = [
        judged(store, slug, lineage)
        for slug in sorted(store.slugs())
        if store.config(slug).state != "stopped"
        for lineage in expert_lineage.every(store, slug)
        if lineage.state == expert_lineage.COMPLETE
    ]
    return found or [NONE]


def main(store: Proofs | None = None) -> int:
    if store is None:
        from scripts.swarm.store import RedisStore, redis_client  # pyright: ignore[reportUnknownVariableType]

        store = RedisStore(redis_client())
    found = lines(store)
    for line in found:
        print(line)
    return int(any(line.startswith(FAIL) for line in found))


if __name__ == "__main__":
    sys.exit(main())
