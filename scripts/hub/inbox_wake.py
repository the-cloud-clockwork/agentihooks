import hashlib
import os
from dataclasses import replace

from scripts.events import deadlines
from scripts.events.bus import Bus
from scripts.events.envelope import Envelope
from scripts.hub.registry import Reactor
from scripts.inbox import wake
from scripts.inbox.store import InboxStore
from scripts.swarm import lease, outage
from scripts.swarm.delivery import HerdrMessenger
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import connect


class InboxWake:
    def __init__(self, bus: Bus, store=None, ledger=None, herdr=None, clock=lease.wall_ms, environ=None):
        self.bus = bus
        self.store = store
        self.ledger = ledger or LedgerClient(service=True)
        self.herdr = herdr or HerdrMessenger()
        self.clock = clock
        self.environ = os.environ if environ is None else environ
        self.state = bus.state("inbox-wake")

    def reactor(self) -> Reactor:
        return Reactor("inbox-wake", "swarm.*.inbox-wake.requested", self.handle, singleton=True)

    def expiry_reactor(self) -> Reactor:
        return Reactor("inbox-wake-expiry", "swarm.*.deadline.expired", self.handle, singleton=True)

    def handle(self, event: Envelope) -> None:
        if event.type == "inbox.wake" and event.entity == "inbox-wake":
            key = self._key(event.swarm, event.key)
            entry = self.state.get(key)
            if entry is None:
                request = deadlines.schedule(event, "inbox", event.time)
                self.state.create(key, request.encode())
            else:
                request = Envelope.decode(entry.value)
            self.bus.events.publish(deadlines.subject(request), request)
        elif event.type == "deadline.expired" and event.data.get("kind") == "inbox":
            self._expire(event)

    def _key(self, swarm: str, item: str) -> str:
        return hashlib.sha256(f"{swarm}\0{item}".encode()).hexdigest()

    def _expire(self, event: Envelope) -> None:
        key = self._key(event.swarm, event.data["key"])
        entry = self.state.get(key)
        if entry is None:
            return
        request = Envelope.decode(entry.value)
        if request.type != "deadline.scheduled" or (event.cause, event.version) != (request.id, request.version):
            return
        self.store = self.store or connect()
        inbox = InboxStore(self.store.redis)
        item = inbox.get(event.data["key"])
        now, window = self.clock(), wake.window_ms(self.environ)
        agents = [agent for agent in self.store.agents(event.swarm) if agent.state != "finished"]
        if item is not None and item.state == "pending" and not item.fyi:
            clock = wake._Clock(now, window, wake.quiet_ms(self.environ))
            wake.wake_item(inbox, event.swarm, agents, self.herdr, self.ledger, item, clock)
        item = inbox.get(event.data["key"])
        due = self._due(inbox, event.swarm, agents, item, now, window)
        source = replace(
            request,
            id=event.id,
            entity="inbox-wake",
            key=event.data["key"],
            version=request.version + 1,
        )
        next_request = deadlines.cancel(source, "inbox") if due is None else deadlines.schedule(source, "inbox", due)
        self.state.update(key, next_request.encode(), entry.revision)
        self.bus.events.publish(deadlines.subject(next_request), next_request)

    def _due(self, inbox, slug, agents, item, now, window):
        if item is None or item.state != "pending" or item.fyi:
            return None
        history = inbox.history(item.id)
        if any(row.get("event") == wake.TO_OPERATOR for row in history):
            return None
        receiver, held = wake._receiver(inbox, item.address)
        since = wake._seat_clock(
            inbox, receiver, held, {agent.name for agent in agents}, wake._swarm_config(inbox, slug)
        )
        if since is None or outage.open_records(slug):
            return now + window
        last = max(since, *(row["at"] for row in history))
        return max(now + 1, last + window + outage.paused_ms(slug, last, now))
