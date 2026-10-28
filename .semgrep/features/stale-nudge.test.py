# ruleid: feature-stale-nudge-in-tick
def _watch_idle(slug, store, ledger, runtime, rows, agent, now_ms):
    observed = runtime.observe(agent)
    claimant = rows.get(agent.task, {}).get("claimed_by") == agent.name
    store.put_agent(slug, agent)
    return []


# ok: feature-stale-nudge-in-tick
def _watch_idle(slug, store, ledger, runtime, rows, agent, now_ms):
    observed = runtime.observe(agent)
    claimant = rows.get(agent.task, {}).get("claimed_by") == agent.name
    nudged = _stale_nudge(slug, store, agent, observed, pending, now_ms) if claimant else []
    store.put_agent(slug, agent)
    return nudged


# ruleid: feature-stale-nudge-in-tick
def _stale_nudge(slug, store, agent, observed, pending, now_ms):
    if agent.lane == MASTER or not agent.task or pending:
        return []
    return []


# ok: feature-stale-nudge-in-tick
def _stale_nudge(slug, store, agent, observed, pending, now_ms):
    if agent.lane == MASTER or not agent.task or pending:
        return []
    return stale_nudge.nudge(InboxStore(store.redis), slug, agent, observed.provider_error, now_ms)
