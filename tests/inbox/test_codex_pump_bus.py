import pytest

from scripts.codex_session import protocol
from scripts.events import memory
from scripts.events.config import BusConfig
from scripts.inbox import codex
from tests.inbox.test_codex_pump import EXECUTION, ME, OWNER, SLUG, THREAD, Rig

pytestmark = pytest.mark.unit

SEAT = f"eng-1@{SLUG}"


class BusRig(Rig):
    def __init__(self):
        from scripts.inbox.bus import SeatFeed

        self.bus_clock = [0]
        self.bus = memory.open_bus(BusConfig(), clock=lambda: self.bus_clock[0])
        self.feed_for = lambda store: SeatFeed(lambda: self.bus, store, ME, OWNER, seat=lambda: SEAT)
        super().__init__()

    def new_pump(self):
        binding = codex.Binding(
            self.connection,
            THREAD,
            ME,
            OWNER,
            codex.Transport(self.bindings, SLUG, EXECUTION),
            reconnect=lambda: protocol.Connection(self.server, 5, clock=self.clock),
        )
        timing = codex.Timing(
            clock=self.clock,
            now_ms=lambda: int(self.clock.now * 1000),
            reconcile_s=30,
            sleep=lambda delay: setattr(self.clock, "now", self.clock.now + delay),
        )
        return codex.Pump(self.store, binding, timing, log=self.logged.append, feed=self.feed_for(self.store))


@pytest.fixture
def rig(recovery_deadline):
    return BusRig()


def test_the_pump_wakes_on_a_bus_delivery_and_confirms_it_once_accepted(rig):
    from scripts.inbox.bus import Message, SeatInbox

    rig.pump.start()
    rig.pump.step()
    assert rig.pump.subscription is None
    item = rig.store.send("alice", ME, "hello")
    rig.pump.step()
    assert rig.server.turns == []
    SeatInbox(rig.bus, SLUG).send(SEAT, Message(item.id, "alice", "hello"))
    rig.pump.step()
    assert rig.server.turns[0]["inputs"][0].endswith("\n\nhello")
    assert rig.store.get(item.id).state == "delivered"
    assert rig.pump.feed.held == {}
    rig.bus_clock[0] += 60_000
    assert SeatInbox(rig.bus, SLUG).consumer(SEAT, "later").fetch(5, 0) == []


def test_a_bus_failure_leaves_the_pump_on_its_redis_reconcile(rig, monkeypatch):
    from scripts.inbox.bus import SeatConsumer

    def lost(consumer, batch, timeout_ms):
        raise RuntimeError("bus connection lost")

    monkeypatch.setattr(SeatConsumer, "fetch", lost)
    item = rig.store.send("alice", ME, "hello")
    assert rig.run(1) == codex.STOPPED
    assert rig.server.turns[0]["inputs"][0].endswith("\n\nhello")
    assert any("bus connection lost" in line for line in rig.logged)
    assert rig.record().state == codex.CONNECTED
    assert rig.store.get(item.id).state == "delivered"
