"""Codex push pump: deliver one recipient's inbox into its bound app-server thread, one submission at a time."""

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from scripts.codex_session import protocol
from scripts.codex_session.binding import CONNECTED, DEGRADED, BindingStore
from scripts.inbox.dispatch import Dispatcher, digest
from scripts.inbox.receipts import MISMATCH, RELEASED, Delivery, DispatchError
from scripts.inbox.store import NOTIFY, InboxStore, Item, now_ms

STOPPED = "stopped"
UNOBSERVED = "an active turn that began before this pump connected"
RECONCILE_S = 30.0
POLL_S = 0.2
BACKOFF_S = 1.0
BACKOFF_MAX_S = 60.0
IN_PROGRESS = "inProgress"
TURN_STARTED, TURN_COMPLETED, RESOLVED = "turn/started", "turn/completed", "serverRequest/resolved"
LOST_OWNER = "{} no longer delivers for {}"
HELD = "thread {} waits on {}; inbox delivery is held"
DEGRADED_LOG = "codex pump for {} degraded: {}: {}"


def envelope(item: Item, delivery: Delivery) -> str:
    return f"[swarm delivery] item {item.id} delivery {delivery.id} from {item.sender}\n\n{item.text}"


def stderr(line: str) -> None:
    print(f"[agentihooks codex-pump] {line}", file=sys.stderr)


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
        record = self.bindings.get(self.slug, self.execution_id)
        self.bindings.put(self.slug, replace(record, **changes))


@dataclass(frozen=True)
class Timing:
    clock: Callable[[], float] = time.monotonic
    now_ms: Callable[[], int] = now_ms
    reconcile_s: float = RECONCILE_S
    poll_s: float = POLL_S
    backoff_s: float = BACKOFF_S
    backoff_max_s: float = BACKOFF_MAX_S


@dataclass(frozen=True)
class Binding:
    connection: protocol.Connection
    thread_id: str
    recipient: str
    owner: str
    transport: Transport


class Pump:
    def __init__(
        self, store: InboxStore, binding: Binding, timing: Timing = Timing(), log: Callable[[str], None] = stderr
    ) -> None:
        self.store = store
        self.dispatcher = Dispatcher(store)
        self.receipts = self.dispatcher.receipts
        self.binding = binding
        self.timing = timing
        self.log = log
        self.state = ThreadState()
        self.queue = []
        self.subscription = None
        self.hold_until = 0.0
        self.backoff = timing.backoff_s
        self.surfaced = ""

    def run(self, stop: Callable[[], bool]) -> str:
        """Pump until stop() or a failure; a failure is logged, marks the transport degraded and returns DEGRADED.

        A failure to write that mark propagates.
        """
        try:
            self.start()
            while not stop():
                self.step()
            return STOPPED
        except Exception as failure:
            self.log(DEGRADED_LOG.format(self.binding.recipient, type(failure).__name__, failure))
            self.binding.transport.mark(state=DEGRADED, last_error_class=type(failure).__name__)
            return DEGRADED
        finally:
            if self.subscription is not None:
                self.subscription.close()

    def start(self) -> None:
        binding = self.binding
        binding.connection.listener = self.state.observe
        self.dispatcher.own(binding.recipient, binding.owner)
        self.receipts.recover(binding.recipient, binding.owner)
        self.subscription = self.store.redis.pubsub()
        self.subscription.subscribe(NOTIFY)
        self._refresh()
        if self.state.active_turn:
            self.state.requests[UNOBSERVED] = UNOBSERVED
        self.next_reconcile = self.timing.clock()
        binding.transport.mark(state=CONNECTED, last_error_class="")

    def step(self) -> None:
        self.binding.connection.poll(self.timing.poll_s)
        woken = self._hinted()
        if self.timing.clock() >= self.next_reconcile:
            self._reconcile()
            woken = True
        if woken:
            self.queue += self.dispatcher.reserve(self.binding.recipient, self.binding.owner)
        self._surface()
        while self.queue and not self.state.requests and self.timing.clock() >= self.hold_until:
            self._submit(self.queue.pop(0))

    def _hinted(self) -> bool:
        if self.subscription.get_message(timeout=0) is None:
            return False
        while self.subscription.get_message(timeout=0) is not None:
            pass
        return True

    def _reconcile(self) -> None:
        binding = self.binding
        if not self.dispatcher.renew(binding.recipient, binding.owner):
            raise DispatchError(LOST_OWNER.format(binding.owner, binding.recipient))
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
        owner = self.binding.owner
        item = self.store.get(delivery.item)
        if digest(item) != delivery.digest:
            self._requeue(delivery, MISMATCH)
            self.next_reconcile = self.timing.clock()
            return
        try:
            self.receipts.submitting(delivery.id, owner)
        except DispatchError as gone:
            self.receipts.reject(delivery.id, owner, str(gone))
            return
        try:
            self._arbitrate(envelope(item, delivery))
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

    def _arbitrate(self, text: str) -> None:
        try:
            self._send(text)
        except protocol.Refused as refused:
            try:
                self._refresh()
            except (protocol.ProtocolError, TimeoutError) as lost:
                raise protocol.Refused(str(refused)) from lost
            self._send(text)

    def _send(self, text: str) -> None:
        connection, thread = self.binding.connection, self.binding.thread_id
        if self.state.active_turn:
            self.state.follow(protocol.steer_turn(connection, thread, self.state.active_turn, text))
        else:
            self.state.follow(protocol.start_turn(connection, thread, text))

    def _hold(self) -> None:
        self.hold_until = self.timing.clock() + self.backoff
        self.next_reconcile = min(self.next_reconcile, self.hold_until)
        self.backoff = min(self.backoff * 2, self.timing.backoff_max_s)
