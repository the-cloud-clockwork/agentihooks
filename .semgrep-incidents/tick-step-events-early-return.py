EVENTS_ENGINE = "events"


def _orphans_step(tick, get):
    # ruleid: tick-step-events-early-return
    if get("config").engine == tick.EVENTS_ENGINE:
        return []
    return tick.skip_refused(tick.orphans, get("slug"))


def _acks_step(tick, get):
    config = get("config")
    # ruleid: tick-step-events-early-return
    if config.engine == EVENTS_ENGINE:
        return None
    return tick.collect(config)


def _reap_step(tick, get):
    # ruleid: tick-step-events-early-return
    if get("config").engine == "events":
        return []
    return tick.reap(get("slug"))


def _orphans_both_engines(tick, get):
    # ok: tick-step-events-early-return
    return tick.skip_refused(tick.orphans, get("slug"))


def _spawn_step(tick, get):
    config = get("config")
    # ok: tick-step-events-early-return
    if config.state == "running" and config.engine != tick.EVENTS_ENGINE:
        return tick.spawn(config)
    return []


def _launch_job(tick, get):
    config = get("config")
    # ok: tick-step-events-early-return
    return [] if config.engine == EVENTS_ENGINE else tick.collect(config)
