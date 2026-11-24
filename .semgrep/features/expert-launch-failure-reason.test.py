def _launch_without_a_reason(slug, store, failed):
    # ruleid: feature-expert-launch-failure-reason
    SEAT.transfers.failed(store, slug, failed.record)
    return []


def _launch_through_the_module(slug, store, failed):
    # ruleid: feature-expert-launch-failure-reason
    transfers.failed(store, slug, failed.record)
    return []


def _launch_with_its_reason(slug, store, failed, why):
    # ok: feature-expert-launch-failure-reason
    SEAT.transfers.failed(store, slug, failed.record, why)
    return []


def _launch_with_a_named_reason(slug, store, failed, why):
    # ok: feature-expert-launch-failure-reason
    transfers.failed(store, slug, failed.record, reason=why)
    return []


def _launch_refused_for_room(slug, store, failed):
    # ok: feature-expert-launch-failure-reason
    SEAT.transfers.detach(store, slug, failed.record)
    return []
