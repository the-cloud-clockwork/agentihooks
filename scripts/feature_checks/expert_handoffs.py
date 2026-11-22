"""Live check of the expert handoffs: in every swarm that is not stopped, the handoff an expert left on the seat of a
running lineage conforms to the expert handoff contract against the saved mission of that lineage. A stored document
with no title line is the runtime's recovery note: it is listed and not judged. The check prints one line per lineage
that holds a document and never an account or a credential."""

import sys
from collections.abc import Iterable
from typing import Any, Protocol

from scripts.handoff import expert
from scripts.swarm import expert_lineage
from scripts.swarm.store import SwarmError

FAIL = "FAIL: "
CONFORMS = "{slug} lineage {id} holds a conforming expert handoff"
RECOVERY = "{slug} lineage {id} holds a recovery note the runtime wrote"
NONE = "no running expert lineage holds a handoff"


class Handoffs(Protocol):
    """What the check reads of the swarm store: the swarms, each one's state, its keys and its stored handoffs."""

    redis: Any

    def slugs(self) -> Iterable[str]: ...

    def config(self, slug: str) -> Any: ...

    def key(self, slug: str, *parts: str) -> str: ...

    def handoff(self, slug: str, task: str) -> str: ...


def judged(store: Handoffs, slug: str, lineage: expert_lineage.Lineage, text: str) -> str:
    """The line for the document a running lineage holds: conforming, failing with every fault, or a recovery note."""
    if not text.lstrip().startswith("# "):
        return RECOVERY.format(slug=slug, id=lineage.id)
    try:
        found = expert.problems(text, _taken, slug, expert_lineage.mission(store, slug, lineage))
    except SwarmError as fault:
        found = [str(fault)]
    if found:
        return f"{FAIL}{slug} lineage {lineage.id}: {'; '.join(found)}"
    return CONFORMS.format(slug=slug, id=lineage.id)


def _taken(address: str) -> bool:
    """Every Read first address resolved when the handoff command took the document, so none is resolved again."""
    return True


def lines(store: Handoffs) -> list[str]:
    """One line per running lineage that holds a document, over the swarms that are not stopped, or one line that
    says none holds one."""
    found = [
        judged(store, slug, lineage, text)
        for slug in sorted(store.slugs())
        if store.config(slug).state != "stopped"
        for lineage in expert_lineage.running(store, slug)
        if (text := store.handoff(slug, lineage.id))
    ]
    return found or [NONE]


def main(store: Handoffs | None = None) -> int:
    if store is None:
        from scripts.swarm.store import RedisStore, redis_client  # pyright: ignore[reportUnknownVariableType]

        store = RedisStore(redis_client())
    found = lines(store)
    for line in found:
        print(line)
    return int(any(line.startswith(FAIL) for line in found))


if __name__ == "__main__":
    sys.exit(main())
