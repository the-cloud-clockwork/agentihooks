"""Run orders: a workflow run with no swarm task, sent to one hive on its own stream beside the launch orders."""

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from scripts.hive import registry
from scripts.hive.orders import RECEIPT_SECONDS, SENT, Ack

if TYPE_CHECKING:
    from redis import Redis

__all__ = ["RunOrder", "RunOrders"]


@dataclass(frozen=True)
class RunOrder:
    run_id: str
    workflow: str
    case: str
    repository: str
    owner: str
    needs: tuple[str, ...]
    hive: str
    expires_at: int

    def encode(self) -> str:
        """The order as the JSON a stream entry carries."""
        return json.dumps(asdict(self))

    @classmethod
    def decode(cls, raw: str) -> "RunOrder":
        """The order a stream entry carries, with its needs back as a tuple."""
        fields = json.loads(raw)
        return cls(**{**fields, "needs": tuple(fields["needs"])})


class RunOrders:
    def __init__(self, redis: "Redis") -> None:
        self.redis = redis

    def stream(self, hive: str) -> str:
        """The hive's run order stream."""
        return f"{registry.key(hive)}:run-orders"

    def receipt(self, order: RunOrder) -> str:
        """The receipt that records one run order's send and its ack."""
        return f"{registry.key(order.hive)}:run-order:{order.run_id}"

    def send(self, hive: str, order: RunOrder) -> str | None:
        """Adds the order to the hive's stream once; a repeat or an order for another hive adds nothing."""
        from redis.exceptions import WatchError

        if order.hive != hive:
            return None
        receipt = self.receipt(order)
        with self.redis.pipeline() as pipe:
            try:
                pipe.watch(receipt)
                if pipe.hexists(receipt, "sent"):
                    return None
                pipe.multi()
                pipe.hset(receipt, mapping=SENT)
                pipe.expire(receipt, RECEIPT_SECONDS)
                pipe.xadd(self.stream(hive), {"order": order.encode()})
                return pipe.execute()[-1]
            except WatchError:
                return None

    def ack(self, order: RunOrder) -> Ack | None:
        """The ack the hive recorded for the order, if any."""
        raw = self.redis.hget(self.receipt(order), "ack")
        return Ack(**json.loads(raw)) if raw else None

    def finish(self, hive: str, item: str, order: RunOrder, ack: Ack) -> None:
        """Records the first ack on the receipt and removes the stream entry."""
        self.redis.hsetnx(self.receipt(order), "ack", ack.encode())
        canonical = self.redis.hget(self.receipt(order), "ack")
        with self.redis.pipeline() as pipe:
            pipe.hset(f"{self.stream(hive)}:acks", item, canonical)
            pipe.xdel(self.stream(hive), item)
            pipe.execute()
