def _holding_wait(store: object, slug: object, found: object, now_ms: object) -> object:
    return found


def raw_wait(tick: object, store: object, slug: object, agent: object, observed: object, now_ms: object) -> object:
    return tick.idle_state.verdict(
        observed.state,
        tick.idle_state.heartbeat(store.redis, slug, agent.name),
        # ruleid: idle-count-trusts-unended-checked-wait
        tick.idle_state.wait(store.redis, slug, agent.name),
        now_ms,
    )


def timed_raw_wait(
    tick: object, store: object, slug: object, agent: object, observed: object, now_ms: object
) -> object:
    return tick.idle_state.verdict(
        observed.state,
        tick.timing.timed(tick.idle_state.heartbeat, store.redis, slug, agent.name),
        # ruleid: idle-count-trusts-unended-checked-wait
        tick.timing.timed(tick.idle_state.wait, store.redis, slug, agent.name),
        now_ms,
    )


def holding_wait(tick: object, store: object, slug: object, agent: object, observed: object, now_ms: object) -> object:
    return tick.idle_state.verdict(
        observed.state,
        tick.timing.timed(tick.idle_state.heartbeat, store.redis, slug, agent.name),
        # ok: idle-count-trusts-unended-checked-wait
        _holding_wait(store, slug, tick.timing.timed(tick.idle_state.wait, store.redis, slug, agent.name), now_ms),
        now_ms,
    )


def hive_lost_raw(tick: object, store: object, slug: object, agent: object, now_ms: object) -> object:
    # ruleid: idle-count-trusts-unended-checked-wait
    wait = tick.idle_state.wait(store.redis, slug, agent.name)
    return wait is None


def hive_lost_held(tick: object, store: object, slug: object, agent: object, now_ms: object) -> object:
    # ok: idle-count-trusts-unended-checked-wait
    wait = _holding_wait(store, slug, tick.idle_state.wait(store.redis, slug, agent.name), now_ms)
    return wait is None
