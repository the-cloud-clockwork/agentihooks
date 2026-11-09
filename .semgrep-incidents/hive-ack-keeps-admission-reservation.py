def finished_without_release(self, request, order, agent):
    ack = self.orders.ack(order)
    if not ack:
        return Outcome("spawn", Status.REFUSED, HIVE, detail=GONE)
    if ack.status != "live":
        # ruleid: hive-ack-keeps-admission-reservation
        return Outcome("spawn", Status.REFUSED, HIVE, detail=f"hive launch failed: {ack.failure}")
    return Outcome("spawn", Status.OK, HIVE)


def complete_without_release(self, order, ack):
    agent = self.store.execution_registry.get(self.slug, order.execution_id)
    if ack.status != "live":
        status = Status.AMBIGUOUS if ack.failure == "ambiguous" else Status.REFUSED
        # ruleid: hive-ack-keeps-admission-reservation
        return Outcome("spawn", status, HIVE, detail=f"hive launch failed: {ack.failure}")
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


def complete_with_release(self, order, ack):
    agent = self.store.execution_registry.get(self.slug, order.execution_id)
    if agent is not None:
        admission.release(self.store.redis, order.hive, agent.name)
    if ack.status != "live":
        # ok: hive-ack-keeps-admission-reservation
        return Outcome("spawn", Status.REFUSED, HIVE, detail=f"hive launch failed: {ack.failure}")
    return Outcome("spawn", Status.OK, HIVE)
