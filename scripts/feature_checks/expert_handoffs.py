"""Live check of the expert handoffs: in every swarm that is not stopped, the handoff an expert left on the seat of a
running lineage conforms to the expert handoff contract against the saved mission of that lineage. Its Read first
addresses resolved when the handoff command took the document and are not resolved again: an address may rightly
stop resolving afterwards. A stored document with no title line is the runtime's recovery note: it is listed and not
judged. The check prints one line per lineage that holds a document and never an account or a credential."""

import sys
from typing import Protocol

from scripts.feature_checks.expert_missions import Missions
from scripts.handoff import expert
from scripts.swarm import expert_lineage
from scripts.swarm.store import SwarmError

FAIL = "FAIL: "
CONFORMS = "{slug} lineage {id} holds a conforming expert handoff"
RECOVERY = "{slug} lineage {id} holds a recovery note the runtime wrote"
NONE = "no running expert lineage holds a handoff"


class Handoffs(Missions, Protocol):
    """What the check reads of the swarm store: what the missions check reads, and the stored handoffs."""

    def handoff(self, slug: str, task: str) -> str: ...


def _judged(store: Handoffs, slug: str, lineage: expert_lineage.Lineage, text: str) -> str:
    """The line for the document a running lineage holds: conforming, failing with every fault, or a recovery note."""
    if not text.lstrip().startswith("# "):
        return RECOVERY.format(slug=slug, id=lineage.id)
    try:
        found = expert.problems(text, _resolved_when_taken, slug, expert_lineage.mission(store, slug, lineage))
    except SwarmError as fault:
        found = [str(fault)]
    if found:
        return f"{FAIL}{slug} lineage {lineage.id}: {'; '.join(found)}"
    return CONFORMS.format(slug=slug, id=lineage.id)


def _resolved_when_taken(address: str) -> bool:
    """A Read first address is taken as resolving: the handoff command resolved it when it took the document."""
    return True


def lines(store: Handoffs) -> list[str]:
    """One line per running lineage that holds a document, over the swarms that are not stopped, or one line that
    says none holds one."""
    found = [
        _judged(store, slug, lineage, text)
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
