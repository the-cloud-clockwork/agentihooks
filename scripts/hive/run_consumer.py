"""Hive run consumer: starts each run order on the hive's stream as a detached workflow run in its project checkout."""

import time
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.hive import daemon
from scripts.hive.orders import DONE, FAILED, Ack
from scripts.hive.run_orders import RunOrder, RunOrders
from scripts.workflows import cli, loader, scheduled, store

if TYPE_CHECKING:
    from redis import Redis

__all__ = ["RunConsumer", "case_file"]

BATCH = 32
CASES = "run-cases"
EXPIRED, REPOSITORY, WORKFLOW = "expired", "unknown_repository", "missing_workflow"
INVALID, IDENTITY, START = "invalid_order", "identity", "start_failed"


def case_file(run_id: str) -> Path:
    """The case file a run order's text is written to before its run starts."""
    return store.default_root().parent / CASES / cli.run_folder(run_id).name


class RunConsumer:
    def __init__(self, redis: "Redis", hive: str) -> None:
        self.redis, self.hive, self.orders = redis, hive, RunOrders(redis)

    def poll(self) -> int:
        """Consumes one batch of the hive's run orders and returns how many entries it read."""
        entries: list[tuple[str, dict[str, str]]] = self.redis.xrange(self.orders.stream(self.hive), count=BATCH)  # pyright: ignore[reportAssignmentType]
        for item, fields in entries:
            self.consume(item, fields)
        return len(entries)

    def consume(self, item: str, fields: dict[str, str]) -> None:
        """Acks one entry once its order started or failed; an order another consumer claimed waits."""
        try:
            order = RunOrder.decode(fields["order"])
            case = case_file(order.run_id)
        except (AttributeError, KeyError, TypeError, ValueError):
            self._drop(item, INVALID)
            return
        if order.hive != self.hive:
            self._drop(item, IDENTITY)
            return
        ack = self.orders.ack(order) or self.apply(order, case)
        if ack is not None:
            self.orders.finish(self.hive, item, order, ack)

    def apply(self, order: RunOrder, case: Path) -> Ack | None:
        """Claims the order by its receipt, then starts its run or names why it cannot start."""
        now = int(time.time() * 1000)
        expired = order.expires_at <= now
        if not self.redis.hsetnx(self.orders.receipt(order), "started", str(now)):
            return Ack(FAILED, {}, EXPIRED) if expired else None
        if expired:
            return Ack(FAILED, {}, EXPIRED)
        checkout = daemon.local_checkout(order.repository)
        if checkout == order.repository:
            return Ack(FAILED, {}, REPOSITORY)
        if order.workflow not in loader.resolve(checkout)[0]:
            return Ack(FAILED, {}, WORKFLOW)
        try:
            case.parent.mkdir(parents=True, exist_ok=True)
            case.write_text(order.case)
            scheduled.start(order.workflow, str(case), checkout, order.run_id, order.owner, hive=self.hive)
        except OSError:
            return Ack(FAILED, {}, START)
        return Ack(DONE, {"host_id": self.hive})

    def _drop(self, item: str, failure: str) -> None:
        stream = self.orders.stream(self.hive)
        with self.redis.pipeline() as pipe:  # pyright: ignore[reportUnknownMemberType]
            pipe.hset(f"{stream}:acks", item, Ack(FAILED, {}, failure).encode())
            pipe.xdel(stream, item)
            pipe.execute()
