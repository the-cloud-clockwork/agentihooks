def spawn_order(tick, timing, pass_args, caps):
    queue = []
    for lane, cap in caps.items():
        # ruleid: claim-order-per-lane
        ready = timing.timed(tick.ready_in_order, *pass_args, lane)
        queue += ready[:cap]
    return queue


def each_lane(pass_args, lanes):
    found = {}
    for lane in lanes:
        # ruleid: claim-order-per-lane
        found[lane] = ready_in_order(*pass_args, lane)
    return found


def once_per_pass(tick, timing, pass_args, caps):
    # ok: claim-order-per-lane
    lanes = timing.timed(tick.ready_by_lane, *pass_args, list(caps))
    queue = []
    for lane, cap in caps.items():
        queue += lanes[lane][:cap]
    return queue


def one_lane(tick, pass_args, lane):
    # ok: claim-order-per-lane
    return tick.ready_in_order(*pass_args, lane)
