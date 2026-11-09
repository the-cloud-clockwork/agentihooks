def finished_without_release(self, request, order, agent):
    ack = self.orders.ack(order)
    if not ack:
        return Outcome("spawn", Status.REFUSED, HIVE, detail=GONE)
    if ack.status != "live":
        # ruleid: hive-ack-keeps-admission-reservation
        return Outcome("spawn", Status.REFUSED, HIVE, detail=f"hive launch failed: {ack.failure}")
    admission.joined(self.store.redis, order.hive, agent.name)
    return Outcome("spawn", Status.OK, HIVE)


def finished_with_release(self, request, order, agent):
    ack = self.orders.ack(order)
    if not ack:
        return Outcome("spawn", Status.REFUSED, HIVE, detail=GONE)
    admission.release(self.store.redis, order.hive, agent.name)
    if ack.status != "live":
        # ok: hive-ack-keeps-admission-reservation
        return Outcome("spawn", Status.REFUSED, HIVE, detail=f"hive launch failed: {ack.failure}")
    return Outcome("spawn", Status.OK, HIVE)


def complete(self, order, ack):
    if ack.status != "live":
        # ok: hive-ack-keeps-admission-reservation
        return Outcome("spawn", Status.REFUSED, HIVE, detail=f"hive launch failed: {ack.failure}")
    return Outcome("spawn", Status.OK, HIVE)
