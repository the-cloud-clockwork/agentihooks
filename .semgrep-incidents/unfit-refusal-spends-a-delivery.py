from scripts.swarm.readiness import UNFIT_NAK_MS


def refused(queue: object, fit: object) -> object:
    while (delivery := queue.take(0)) is not None:
        if not fit(delivery.event):
            # ruleid: unfit-refusal-spends-a-delivery
            delivery.nak(UNFIT_NAK_MS)
            continue
        return delivery
    return None


def counted(queue: object, config: object) -> object:
    delivery = queue.take(0)
    if not config.fit(delivery.event):
        # ruleid: unfit-refusal-spends-a-delivery
        delivery.nak(5_000)
    return delivery


def released(queue: object, fit: object) -> object:
    while (delivery := queue.take(0)) is not None:
        if not fit(delivery.event):
            # ok: unfit-refusal-spends-a-delivery
            queue.release(delivery, UNFIT_NAK_MS)
            continue
        return delivery
    return None


def failed(delivery: object) -> None:
    # ok: unfit-refusal-spends-a-delivery
    delivery.nak(30_000)
