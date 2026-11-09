def raw_wait(tick, store, slug, agent, observed, now_ms):
    return tick.idle_state.verdict(
        observed.state,
        tick.idle_state.heartbeat(store.redis, slug, agent.name),
        # ruleid: idle-count-trusts-unended-checked-wait
        tick.idle_state.wait(store.redis, slug, agent.name),
        now_ms,
    )


def timed_raw_wait(tick, store, slug, agent, observed, now_ms):
    return tick.idle_state.verdict(
        observed.state,
        tick.timing.timed(tick.idle_state.heartbeat, store.redis, slug, agent.name),
        # ruleid: idle-count-trusts-unended-checked-wait
        tick.timing.timed(tick.idle_state.wait, store.redis, slug, agent.name),
        now_ms,
    )


def holding_wait(tick, store, slug, agent, observed, now_ms):
    # ok: idle-count-trusts-unended-checked-wait
    return tick.idle_state.verdict(
        observed.state,
        tick.timing.timed(tick.idle_state.heartbeat, store.redis, slug, agent.name),
        tick.timing.timed(_holding_wait, store, slug, agent.name, now_ms),
        now_ms,
    )


def hive_lost_raw(tick, store, slug, agent, now_ms):
    # ruleid: idle-count-trusts-unended-checked-wait
    wait = tick.idle_state.wait(store.redis, slug, agent.name)
    return wait is None


def _holding_wait(tick, store, slug, name, now_ms):
    # ok: idle-count-trusts-unended-checked-wait
    found = tick.idle_state.wait(store.redis, slug, name)
    return found
