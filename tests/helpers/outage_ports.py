"""Builds the ports the master outage pass writes through over a real swarm store and a ledger client, for tests
that run the pass directly."""

from typing import Any

from scripts.inbox.store import InboxStore
from scripts.swarm import tick_master
from scripts.swarm.store import RedisStore
from scripts.tick.adapters.calls import SpawnStoreCalls
from scripts.tick.adapters.coordination import StoreCoordination
from scripts.tick.adapters.inbox import StoreInbox
from scripts.tick.adapters.ledger import PassLedger
from scripts.tick.deadline import Deadline


def outage_ports(store: RedisStore, slug: str, ledger: Any) -> tick_master.Ports:
    """Return outage pass ports writing to ``store`` through its coordination port and to the ledger client
    ``ledger`` through the pass ledger port, under a deadline that never ends."""
    deadline = Deadline(60, lambda: 0)
    inbox = StoreInbox(InboxStore(store.redis), f"tick@{slug}")
    calls = SpawnStoreCalls(StoreCoordination(store, slug, "tick"), deadline, inbox)
    return tick_master.Ports(calls, inbox, PassLedger(slug, ledger), deadline)
