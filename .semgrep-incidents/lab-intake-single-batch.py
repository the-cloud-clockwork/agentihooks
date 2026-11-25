import time
from typing import Any


class SingleBatch:
    def __init__(self, consumer: Any) -> None:
        self.consumer = consumer

    def heads(self) -> list[Any]:
        heads = []
        # ruleid: lab-intake-single-batch
        for delivery in self.consumer.fetch(50, 100):
            heads.append(delivery)
        return heads


class BoundedDrain:
    def __init__(self, consumer: Any) -> None:
        self.consumer = consumer

    def heads(self) -> list[Any]:
        deadline = time.monotonic() + 10
        heads = []
        while time.monotonic() < deadline:
            # ok: lab-intake-single-batch
            deliveries = self.consumer.fetch(50, 100)
            heads.extend(deliveries)
            if len(deliveries) < 50:
                break
        return heads
