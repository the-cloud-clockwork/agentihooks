"""Codex push pump: deliver one recipient's inbox into its bound app-server thread, one submission at a time."""

from __future__ import annotations

import math
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from scripts.codex_session import protocol
from scripts.codex_session.binding import CONNECTED, DEGRADED, BindingStore, TransportRecord
from scripts.inbox.bus import FeedError, SeatFeed, feed_for
from scripts.inbox.dispatch import Dispatcher, digest
from scripts.inbox.receipts import MISMATCH, RELEASED, Delivery, DispatchError, InboxContention
from scripts.inbox.store import NOTIFY, InboxStore, Item, now_ms, owner_ttl_s
from scripts.swarm import stale_nudge
from scripts.swarm.delivery import envelope as delivery_envelope

if TYPE_CHECKING:
    from redis.client import Pipeline

STOPPED = "stopped"
RECONCILE_S = 10.0
POLL_S = 0.2
BACKOFF_S = 1.0
BACKOFF_MAX_S = 15.0
IN_PROGRESS = "inProgress"
TURN_STARTED, TURN_COMPLETED, RESOLVED = "turn/started", "turn/completed", "serverRequest/resolved"
LOST_OWNER = "{} no longer delivers for {}"
HELD = "thread {} waits on {}; inbox delivery is held"
DEGRADED_LOG = "codex pump for {} degraded: {}: {}"
CONTENDED_LOG = "codex pump for {} retries inbox contention: {}"
BUS_LOST = "codex pump for {} lost its bus inbox, Redis reconcile delivers meanwhile: {}"


def steady() -> None:
    pass


def envelope(item: Item, delivery: Delivery) -> str:
    return delivery_envelope(item, delivery.id, delivery.recipient)


def stderr(line: str) -> None:
    print(f"[agentihooks codex-pump] {line}", file=sys.stderr)


class StaleExecution(DispatchError):
    pass


@dataclass
class ThreadState:
    """The active turn and open server requests, folded from events and refreshed from thread/read."""

    active_turn: str = ""
    requests: dict = field(default_factory=dict)
    ended: str = ""

    def observe(self, message: dict) -> None:
        method, params = message.get("method", ""), message.get("params") or {}
        turn = (params.get("turn") or {}).get("id", "")
        if "id" in message:
            self.requests[message["id"]] = method
        elif method == TURN_STARTED:
            self.active_turn = turn
            self.requests.clear()
        elif method == TURN_COMPLETED:
            self.ended = turn
            if turn == self.active_turn:
                self.active_turn = ""
                self.requests.clear()
        elif method == RESOLVED:
            self.requests.pop(params.get("requestId"), None)

    def follow(self, turn_id: str) -> None:
        if turn_id != self.ended:
            self.active_turn = turn_id
        elif self.active_turn == turn_id:
            self.active_turn = ""

    def refresh(self, thread: dict) -> None:
        turns = thread.get("turns") or [{}]
        last = turns[-1]
        self.active_turn = last.get("id", "") if last.get("status") == IN_PROGRESS else ""
        if not self.active_turn:
            self.requests.clear()


@dataclass(frozen=True)
class Transport:
    bindings: BindingStore
    slug: str
    execution_id: str

    def mark(self, **changes) -> None:
        import json
        from dataclasses import asdict

        from scripts.codex_session.binding import IDENTITY, RECORDS, BindingError
        from scripts.inbox.receipts import transact

        key = self.bindings.store.key(self.slug, RECORDS)

        def update(pipe):
            pipe.watch(key)
            record = self.require(pipe)
            updated = replace(record, **changes)
            if any(getattr(record, field) != getattr(updated, field) for field in IDENTITY):
                raise BindingError("transport record identity is immutable")
            pipe.multi()
            pipe.hset(key, self.execution_id, json.dumps(asdict(updated)))

        transact(self.bindings.redis, update)

    def require(self, pipe: Pipeline | None = None) -> TransportRecord:
        from scripts.codex_session.binding import EXITED, FAILED, STOOD_DOWN

        store = self.bindings.store
        if pipe is not None:
            from scripts.codex_session.binding import RECORDS

            pipe.watch(store.key(self.slug, "executions"), store.key(self.slug, RECORDS))
        record = self.bindings.get(self.slug, self.execution_id)
        if record is None or record.state in {EXITED, FAILED, STOOD_DOWN}:
            raise StaleExecution("execution is no longer current")
        current = store.execution_registry.occupants(self.slug, pipe).get(record.seat)
        if (
            current is None
            or (current.execution_id, current.generation, current.name, current.task)
            != (self.execution_id, record.execution_generation, record.agent_name, record.task_id)
            or current.state == "finished"
        ):
            raise StaleExecution("execution is no longer current")
        return record

    def publish(self, send: Callable[[], None]) -> None:
        from scripts.codex_session.binding import PUBLISHING
        from scripts.inbox.receipts import transact

        key = self.bindings.store.key(self.slug, PUBLISHING)

        def reserve(pipe):
            pipe.watch(key)
            record = self.require(pipe)
            if pipe.hget(key, record.seat):
                raise protocol.ProtocolError("execution already has an inbox publication in flight")
            pipe.multi()
            pipe.hset(key, record.seat, self.execution_id)
            return record

        record = transact(self.bindings.redis, reserve)
        try:
            send()
        finally:

            def release(pipe):
                pipe.watch(key)
                held = pipe.hget(key, record.seat)
                pipe.multi()
                if held == self.execution_id:
                    pipe.hdel(key, record.seat)

            transact(self.bindings.redis, release)


@dataclass(frozen=True)
class Timing:
    clock: Callable[[], float] = time.monotonic
    now_ms: Callable[[], int] = now_ms
    reconcile_s: float = RECONCILE_S
    poll_s: float = POLL_S
    backoff_s: float = BACKOFF_S
    backoff_max_s: float = BACKOFF_MAX_S
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class Binding:
    connection: protocol.Connection
    thread_id: str
    recipient: str
    owner: str
    transport: Transport
    reconnect: Callable[[], protocol.Connection] | None = None


class Pump:
    def __init__(
        self,
        store: InboxStore,
        binding: Binding,
        timing: Timing = Timing(),
        log: Callable[[str], None] = stderr,
        feed: SeatFeed | None = None,
    ) -> None:
        self.store = store
        self.feed = feed
        self.dispatcher = Dispatcher(store)
        self.receipts = self.dispatcher.receipts
        self.receipts.authority = binding.transport.require
        self.binding = binding
        self.timing = timing
        self.log = log
        self.state = ThreadState()
        self.queue = []
        self.subscription = None
        self.hold_until = 0.0
        self.backoff = timing.backoff_s
        self.surfaced = ""
        self.leased = False

    def run(self, stop: Callable[[], bool]) -> str:
        resume = self.start
        backoff, pause = self.timing.backoff_s, 0.0
        try:
            while True:
                try:
                    if pause:
                        self._pause(pause)
                        pause = 0.0
                    resume()
                    resume = steady
                    if stop():
                        return STOPPED
                    self.step()
                except StaleExecution as failure:
                    self.log(DEGRADED_LOG.format(self.binding.recipient, type(failure).__name__, failure))
                    return DEGRADED
                except InboxContention as failure:
                    self.log(CONTENDED_LOG.format(self.binding.recipient, failure))
                    resume = self._resync if resume is steady else resume
                except DispatchError as failure:
                    self._degrade(failure)
                    return DEGRADED
                except Exception as failure:
                    self._degrade(failure)
                    resume = self._reconnect
                else:
                    backoff = self.timing.backoff_s
                    continue
                if stop():
                    return STOPPED
                pause, backoff = backoff, min(backoff * 2, self.timing.backoff_max_s)
        finally:
            self._close()

    def _close(self) -> None:
        if self.subscription is not None:
            self.subscription.close()
        if self.feed is not None:
            self.feed.close()
        self.binding.connection.close()

    def _pause(self, delay: float) -> None:
        whole, rest = divmod(delay, self.timing.reconcile_s)
        for piece in [self.timing.reconcile_s] * int(whole) + ([rest] if rest else []):
            if self.leased:
                self._renew()
            self.timing.sleep(piece)

    def _lease_s(self) -> int:
        return max(owner_ttl_s(), math.ceil(2 * self.timing.reconcile_s))

    def _renew(self) -> None:
        binding = self.binding
        if not self.dispatcher.renew(binding.recipient, binding.owner, ttl_s=self._lease_s()):
            raise DispatchError(LOST_OWNER.format(binding.owner, binding.recipient))

    def _degrade(self, failure: Exception) -> None:
        from redis.exceptions import RedisError

        self.log(DEGRADED_LOG.format(self.binding.recipient, type(failure).__name__, failure))
        try:
            self.binding.transport.mark(state=DEGRADED, last_error_class=type(failure).__name__)
        except (RedisError, StaleExecution):
            pass

    def _reconnect(self) -> None:
        from scripts.codex_session.supervisor import UNIX

        if self.subscription is not None:
            self.subscription.close()
            self.subscription = None
        self.binding.connection.close()
        record = self.binding.transport.require()
        if self.binding.reconnect is not None:
            connection = self.binding.reconnect()
        else:
            connection, _ = protocol.connect(record.endpoint_ref.removeprefix(UNIX), 5.0)
        self.binding = replace(self.binding, connection=connection)
        self.state = ThreadState()
        self.queue.clear()
        self.start()

    def _resync(self) -> None:
        self.queue.clear()
        self.receipts.recover(self.binding.recipient, self.binding.owner)
        self.next_reconcile = self.timing.clock()

    def start(self) -> None:
        binding = self.binding
        binding.transport.require()
        binding.connection.listener = self.state.observe
        self.dispatcher.own(binding.recipient, binding.owner, ttl_s=self._lease_s())
        self.leased = True
        self.receipts.recover(binding.recipient, binding.owner)
        if self.feed is None:
            if self.subscription is not None:
                self.subscription.close()
            self.subscription = self.store.redis.pubsub()
            self.subscription.subscribe(NOTIFY)
        protocol.resume_thread(binding.connection, binding.thread_id)
        self._refresh()
        self.next_reconcile = self.timing.clock()
        binding.transport.mark(state=CONNECTED, last_error_class="")

    def step(self) -> None:
        self.binding.transport.require()
        self.binding.connection.poll(self.timing.poll_s)
        woken = self._hinted()
        if self.timing.clock() >= self.next_reconcile:
            self._reconcile()
            woken = True
        if woken:
            self.queue += self.dispatcher.reserve(self.binding.recipient, self.binding.owner)
        self._surface()
        if self.queue and not self.state.requests and self.timing.clock() >= self.hold_until:
            self._submit_nudge()
        while self.queue and not self.state.requests and self.timing.clock() >= self.hold_until:
            self._submit(self.queue.pop(0))
        if self.feed is not None:
            self._fed(self.feed.settle)

    def _submit_nudge(self) -> None:
        nudge = next((d for d in self.queue if d.ref.startswith(stale_nudge.REF) and self._jumps(d)), None)
        if nudge is None:
            return
        self.queue.remove(nudge)
        self._submit(nudge)

    def _jumps(self, delivery: Delivery) -> bool:
        return stale_nudge.is_nudge(self.store.get(delivery.item))

    def _fed(self, read: Callable[[], object]) -> object:
        try:
            return read()
        except FeedError as failure:
            self.log(BUS_LOST.format(self.binding.recipient, failure))
            return None

    def _hinted(self) -> bool:
        if self.feed is not None:
            return bool(self._fed(lambda: self.feed.wait(0)))
        if self.subscription.get_message(timeout=0) is None:
            return False
        while self.subscription.get_message(timeout=0) is not None:
            pass
        return True

    def _reconcile(self) -> None:
        binding = self.binding
        self._renew()
        self._refresh()
        binding.transport.mark(last_reconcile_at=self.timing.now_ms(), active_turn_id=self.state.active_turn)
        self.next_reconcile = self.timing.clock() + self.timing.reconcile_s

    def _refresh(self) -> None:
        self.state.refresh(protocol.read_thread(self.binding.connection, self.binding.thread_id))

    def _surface(self) -> None:
        pending = next(iter(self.state.requests.values()), "")
        if pending == self.surfaced:
            return
        self.surfaced = pending
        self.binding.transport.mark(pending_request=pending)
        if pending:
            self.log(HELD.format(self.binding.thread_id, pending))

    def _submit(self, delivery: Delivery) -> None:
        self._renew()
        owner = self.binding.owner
        item = self.store.get(delivery.item)
        if digest(item) != delivery.digest:
            self._requeue(delivery, MISMATCH)
            self.next_reconcile = self.timing.clock()
            return
        try:
            self.receipts.submitting(delivery.id, owner)
        except InboxContention:
            raise
        except DispatchError as gone:
            self.receipts.reject(delivery.id, owner, str(gone))
            return
        try:
            self._arbitrate(envelope(item, delivery), stale_nudge.interrupts(item, self.timing.now_ms()))
        except protocol.Refused as refused:
            self._requeue(delivery, str(refused))
            self._hold()
            return
        except (protocol.ProtocolError, TimeoutError):
            self.receipts.recover(self.binding.recipient, owner)
            self.queue.clear()
            raise
        self.receipts.accept(delivery.id, owner, delivery.digest)
        self.backoff = self.timing.backoff_s
        self.binding.transport.mark(active_turn_id=self.state.active_turn, last_transport_activity=self.timing.now_ms())

    def _requeue(self, delivery: Delivery, reason: str) -> None:
        """Reject delivery and release every queued successor, so the next scan rebuilds the queue in inbox order."""
        owner = self.binding.owner
        self.receipts.reject(delivery.id, owner, reason)
        for queued in self.queue:
            self.receipts.reject(queued.id, owner, RELEASED)
        self.queue.clear()

    def _arbitrate(self, text: str, fresh: bool) -> None:
        try:
            self._send(text, fresh)
        except protocol.Refused as refused:
            try:
                self._refresh()
            except (protocol.ProtocolError, TimeoutError) as lost:
                raise protocol.Refused(str(refused)) from lost
            self._send(text, fresh)

    def _send(self, text: str, fresh: bool) -> None:
        def send():
            connection, thread = self.binding.connection, self.binding.thread_id
            if self.state.active_turn and fresh:
                protocol.interrupt_turn(connection, thread, self.state.active_turn)
                self.state.active_turn = ""
                self.state.requests.clear()
            if self.state.active_turn:
                self.state.follow(protocol.steer_turn(connection, thread, self.state.active_turn, text))
            else:
                self.state.follow(protocol.start_turn(connection, thread, text))

        self.binding.transport.publish(send)

    def _hold(self) -> None:
        self.hold_until = self.timing.clock() + self.backoff
        self.next_reconcile = min(self.next_reconcile, self.hold_until)
        self.backoff = min(self.backoff * 2, self.timing.backoff_max_s)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import signal
    from threading import Event

    from scripts.codex_session.supervisor import UNIX
    from scripts.inbox.store import connect as inbox_connect
    from scripts.swarm.store import connect as swarm_connect

    parser = argparse.ArgumentParser(prog="python -m scripts.inbox.codex")
    parser.add_argument("swarm")
    parser.add_argument("execution")
    args = parser.parse_args(argv)
    transport = Transport(BindingStore(swarm_connect()), args.swarm, args.execution)
    record = transport.require()
    connection, _ = protocol.connect(record.endpoint_ref.removeprefix(UNIX), 5.0)
    binding = Binding(connection, record.thread_id, record.agent_name, record.execution_id, transport)
    stopped = Event()
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, lambda number, frame: stopped.set())
    store = inbox_connect()
    feed = feed_for(store, record.agent_name, record.execution_id)
    return 0 if Pump(store, binding, feed=feed).run(stopped.is_set) == STOPPED else 1


if __name__ == "__main__":
    sys.exit(main())
