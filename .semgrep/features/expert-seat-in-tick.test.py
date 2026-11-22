# ruleid: feature-expert-seat-in-tick
def _expert_seats(tick, get):
    return []


# ok: feature-expert-seat-in-tick
def _expert_seats(tick, get):
    if tick.panic.state(get("store"), get("slug")) is not None:
        return []
    actions: list[str] = tick.skip_refused(
        SEAT.expert_seat.run, get("slug"), get("config"), get("store"), get("runtime"), get("now_ms")
    )
    return actions


# ok: feature-expert-seat-in-tick
def _expert_seats(tick, get):
    if tick.panic.state(get("store"), get("slug")) is not None:
        return []
    return tick.skip_refused(SEAT.expert_seat.run, get("slug"), get("config"), get("store"), get("runtime"))


# ruleid: feature-expert-seat-in-tick
def _expert_seats(tick, get):
    if tick.panic.state(get("store"), get("slug")) is not None:
        return []
    actions: list[str] = tick.skip_refused(SEAT.dispatch_seat.run, get("slug"), get("config"), get("store"))
    return actions


# ruleid: feature-expert-seat-in-tick
def _expert_seats(tick, get):
    if tick.panic.state(get("store"), get("slug")) is not None:
        return []
    return SEAT.expert_seat.run(get("slug"), get("config"), get("store"), get("runtime"), get("now_ms"))


# ok: feature-expert-seat-in-tick
def _dispatch_seat(tick, get):
    return []
