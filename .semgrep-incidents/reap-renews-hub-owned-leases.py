from scripts.hub import owned_leases
from scripts.hub.reconciler import legacy_agents


def _reap(slug, store, timed):
    # ruleid: reap-renews-hub-owned-leases
    agents = list(timed(legacy_agents, store, slug))
    return [agent.name for agent in agents]


def reap_all(slug, store):
    # ruleid: reap-renews-hub-owned-leases
    return legacy_agents(store, slug)


def _reap_renewing(slug, store, timed, lease_ms):
    # ok: reap-renews-hub-owned-leases
    agents = list(timed(legacy_agents, store, slug))
    timed(owned_leases.renew, store, slug, agents, lease_ms)
    return [agent.name for agent in agents]


def watched(store, slug):
    # ok: reap-renews-hub-owned-leases
    return legacy_agents(store, slug)
