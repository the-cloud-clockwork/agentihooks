import hashlib
import os
import secrets
from dataclasses import replace

from scripts.events import deadlines
from scripts.events.bus import Bus
from scripts.events.envelope import Envelope
from scripts.hub import lease
from scripts.hub.registry import Reactor
from scripts.inbox import wake
from scripts.inbox.bus import Message, SeatInbox
from scripts.inbox.seats import is_seat
from scripts.inbox.store import REDELIVERED, REDELIVERER, InboxStore, redelivery_ms
from scripts.swarm import outage
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
        return Reactor("inbox-wake", "swarm.*.inbox-wake.requested", self.handle, singleton=True, poll=self.flush)

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

    def flush(self, guard=lambda: True) -> None:
        self.store = self.store or connect()
        for inbox in InboxStore(self.store.redis).worlds():
            key = inbox.key("wake-outbox")
            members = list(inbox.redis.smembers(key))
            for item_id in secrets.SystemRandom().sample(members, min(32, len(members))):
                if not guard():
                    return
                item = inbox.get(item_id)
                swarm = item.address.partition("@")[2] if is_seat(item.address) else inbox.names.slug_of(item.address)
                if swarm:
                    SeatInbox(self.bus, swarm).send(
                        item.address, Message(item.id, item.sender, item.text, item.ref, item.fyi, item.task)
                    )
                inbox.redis.srem(key, item_id)

    def _mail(self, item_id):
        inbox = InboxStore(self.store.redis)
        for world in inbox.worlds():
            if world.redis.exists(world.key("item", item_id)):
                return world, world.get(item_id)
        return inbox, None

    def _expire(self, event: Envelope) -> None:
        key = self._key(event.swarm, event.data["key"])
        entry = self.state.get(key)
        if entry is None:
            return
        request = Envelope.decode(entry.value)
        if request.cause == event.id:
            self.bus.events.publish(deadlines.subject(request), request)
            return
        if request.type != "deadline.scheduled" or (event.cause, event.version) != (request.id, request.version):
            return
        self.store = self.store or connect()
        inbox, item = self._mail(event.data["key"])
        now, window = self.clock(), wake.window_ms(self.environ)
        if item is not None and item.state == "delivered":
            inbox.requeue(item.id, REDELIVERER, REDELIVERED, now - redelivery_ms(self.environ))
            item = inbox.get(item.id)
        agents = [agent for agent in self.store.agents(event.swarm) if agent.state != "finished"]
        if item is not None and item.state == "pending" and not item.fyi:
            clock = wake._Clock(now, window, wake.quiet_ms(self.environ))
            wake.wake_item(inbox, event.swarm, agents, self.herdr, self.ledger, item, clock)
        inbox, item = self._mail(event.data["key"])
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
        if item is not None and item.state == "delivered":
            return max(now + window, item.updated_at + redelivery_ms(self.environ))
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
        due = last + window + outage.paused_ms(slug, last, now)
        return due if due > now else now + window
