from collections.abc import Callable
from typing import Any

from scripts.hub import owned_leases
from scripts.hub.reconciler import legacy_agents


def _reap(slug: str, store: Any, timed: Callable[..., Any]) -> list[str]:
    # ruleid: reap-renews-hub-owned-leases
    agents = list(timed(legacy_agents, store, slug))
    return [agent.name for agent in agents]


def reap_all(slug: str, store: Any) -> Any:
    # ruleid: reap-renews-hub-owned-leases
    return legacy_agents(store, slug)


def _reap_renewing(slug: str, store: Any, timed: Callable[..., Any], lease_ms: int) -> list[str]:
    # ok: reap-renews-hub-owned-leases
    agents = list(timed(legacy_agents, store, slug))
    timed(owned_leases.renew, store, slug, agents, lease_ms)
    return [agent.name for agent in agents]


def watched(store: Any, slug: str) -> Any:
    # ok: reap-renews-hub-owned-leases
    return legacy_agents(store, slug)
