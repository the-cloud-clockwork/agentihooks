"""A master held at the context hard gate past the bound: its handoff never landed, so the tick recycles it in its
seat with a recovery handoff and the next tick starts its successor."""

from hooks.context import context_recycle
from scripts.swarm import affinity, master_stall
from scripts.swarm.health import findings as health
from scripts.swarm.store import MASTER, RedisStore, SwarmConfig

BOUND_MINUTES = 15
GATED = "gated"
RECOVERY = """## Intent
Continue as the master of {slug}; {name} sat {minutes} minutes at the context hard gate without a handoff landing, \
and the swarm recycled it.

## Done
unknown: {name} left no handoff document before it was recycled.

## Stopped at
unknown: {name} was held at the context hard gate and never reported its last step.

## Decisions and promises
unknown: none recorded outside the ledger and the master inbox.

## Next
Re-read the ledger and the master inbox, then continue as master.

## Read first
- inbox:master

<!-- handoff complete -->
"""


def run(slug: str, config: SwarmConfig, store: RedisStore, ledger: object, runtime: object, now_ms: int) -> list[str]:
    """Recycle the live master once its session has sat at the hard gate for the bound."""
    master = affinity.live_master(store, slug)
    if master is None or master.lane != MASTER or config.state in {"stopping", "stopped"}:
        return []
    since = context_recycle.gated_since(master.conversation_id, config.compact_limit)
    if since is None or now_ms - since < BOUND_MINUTES * health.MINUTE_MS:
        return []
    minutes = (now_ms - since) // health.MINUTE_MS
    text = RECOVERY.format(slug=slug, name=master.name, minutes=minutes)
    master_stall.recycle(slug, store, ledger, master, text, now_ms, GATED)
    return [f"recycled master {master.name} in its seat after {minutes} minutes held at the context hard gate"]
