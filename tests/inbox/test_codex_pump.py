import json

import pytest

from scripts.codex_session import protocol
from scripts.codex_session.binding import CONNECTED, DEGRADED, BindingStore, TransportRecord
from scripts.inbox import codex
from scripts.inbox.dispatch import Dispatcher
from scripts.inbox.receipts import MISMATCH, RELEASED, DispatchError, Receipts
from scripts.inbox.store import InboxStore
from scripts.swarm.store import RedisStore, SwarmConfig

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

SLUG = "fixture"
ME = "engineer@abc-0001"
OWNER = "host:41"
THREAD = "thread-1"
EXECUTION = "exe-one"
APPROVAL = "item/commandExecution/requestApproval"


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class AppServer:
    """One scripted app-server thread behind the Connection's socket interface."""

    def __init__(self, clock):
        self.clock = clock
        self.frames = []
        self.sent = []
        self.turns = []
        self.silent = set()
        self.broken = None
        self.early = []

    @property
    def active(self):
        return self.turns[-1]["id"] if self.turns and self.turns[-1]["status"] == codex.IN_PROGRESS else ""

    def send(self, text):
        message = json.loads(text)
        self.sent.append(message)
        if "id" not in message or message["method"] in self.silent:
            return
        result = getattr(self, message["method"].replace("/", "_"))(message["params"])
        if isinstance(result, str):
            reply = {"id": message["id"], "error": {"code": -32600, "message": result}}
        else:
            reply = {"id": message["id"], "result": result}
        self.frames[0:0] = [*self.early, reply]
        self.early = []

    def recv(self, timeout):
        if self.broken is not None:
            raise self.broken
        if not self.frames:
            self.clock.now += timeout
            raise TimeoutError("nothing to read")
        frame = self.frames.pop(0)
        return frame if isinstance(frame, str) else json.dumps(frame)

    def close(self):
        pass

    def turn_start(self, params):
        if not self.active:
            self.turns.append({"id": f"turn-{len(self.turns) + 1}", "status": codex.IN_PROGRESS, "inputs": []})
        self.turns[-1]["inputs"].append(params["input"][0]["text"])
        return {"turn": {"id": self.active}}

    def turn_steer(self, params):
        if params["expectedTurnId"] != self.active:
            return "expected turn mismatch"
        self.turns[-1]["inputs"].append(params["input"][0]["text"])
        return {"turnId": self.active}

    def thread_read(self, params):
        turns = [{"id": turn["id"], "status": turn["status"]} for turn in self.turns]
        return {"thread": {"id": params["threadId"], "turns": turns}}

    def begin_elsewhere(self):
        self.turns.append({"id": f"turn-{len(self.turns) + 1}", "status": codex.IN_PROGRESS, "inputs": []})
        self.frames.append({"method": codex.TURN_STARTED, "params": {"turn": {"id": self.active}}})

    def finish(self, announce=True):
        turn = self.turns[-1]
        turn["status"] = "completed"
        if announce:
            self.frames.append({"method": codex.TURN_COMPLETED, "params": {"turn": {"id": turn["id"]}}})

    def methods(self):
        return [message.get("method") for message in self.sent]


class Rig:
    def __init__(self):
        import fakeredis

        server = fakeredis.FakeServer()
        self.store = InboxStore(fakeredis.FakeRedis(server=server, decode_responses=True))
        swarm = RedisStore(fakeredis.FakeRedis(server=server, decode_responses=True))
        swarm.create(SwarmConfig(SLUG, "agentihooks", 1, 0))
        self.bindings = BindingStore(swarm)
        self.bindings.reserve(SLUG, TransportRecord(EXECUTION, 1, f"eng-1@{SLUG}", ME, "task", "unix:///run/cx/s"))
        self.clock = Clock()
        self.server = AppServer(self.clock)
        self.connection = protocol.Connection(self.server, 5, clock=self.clock)
        self.logged = []
        self.pump = self.new_pump()

    def new_pump(self):
        binding = codex.Binding(self.connection, THREAD, ME, OWNER, codex.Transport(self.bindings, SLUG, EXECUTION))
        timing = codex.Timing(clock=self.clock, now_ms=lambda: int(self.clock.now * 1000), reconcile_s=30)
        return codex.Pump(self.store, binding, timing, log=self.logged.append)

    def record(self):
        return self.bindings.get(SLUG, EXECUTION)

    def run(self, steps):
        left = iter(range(steps))
        return self.pump.run(lambda: next(left, None) is None)

    def lose_notifies(self):
        while self.pump.subscription.get_message(timeout=0) is not None:
            pass


@pytest.fixture
def rig():
    return Rig()


@pytest.fixture(autouse=True)
def watchdog(recovery_deadline):
    yield


def deliveries(rig, item_id):
    receipts = Receipts(rig.store)
    keys = rig.store.redis.keys(rig.store.key("delivery", "*"))
    return [d for d in (receipts.get(key.rsplit(":", 1)[1]) for key in keys) if d.item == item_id]


def test_an_idle_thread_wakes_with_turn_start_and_no_wait_tool(rig):
    item = rig.store.send("alice", ME, "hello")
    assert rig.run(1) == codex.STOPPED
    [delivery] = deliveries(rig, item.id)
    assert [m for m in rig.server.methods() if m.startswith("turn/")] == ["turn/start"]
    assert rig.server.turns[0]["inputs"] == [
        f"[swarm delivery] item {item.id} delivery {delivery.id} from alice\n\nhello"
    ]
    assert [m["params"]["threadId"] for m in rig.server.sent if m["method"] == "turn/start"] == [THREAD]
    assert rig.store.get(item.id).state == "delivered"
    assert (delivery.state, delivery.committed) == ("accepted", True)
    assert (rig.record().state, rig.record().active_turn_id) == (CONNECTED, "turn-1")
    assert rig.record().last_transport_activity == int(rig.clock.now * 1000)


def test_busy_delivery_steers_the_observed_turn_on_the_same_thread(rig):
    rig.pump.start()
    rig.pump.step()
    rig.server.begin_elsewhere()
    item = rig.store.send("alice", ME, "steer me")
    rig.pump.step()
    steer = [m for m in rig.server.sent if m.get("method") == "turn/steer"]
    assert [m["params"]["threadId"] for m in steer] == [THREAD]
    assert [m["params"]["expectedTurnId"] for m in steer] == ["turn-1"]
    assert len(rig.server.turns) == 1
    [delivery] = deliveries(rig, item.id)
    assert rig.server.turns[0]["inputs"][-1] == (
        f"[swarm delivery] item {item.id} delivery {delivery.id} from alice\n\nsteer me"
    )
    assert rig.store.get(item.id).state == "delivered"


def test_a_busy_thread_seen_only_through_its_started_event_is_steered(rig):
    rig.pump.start()
    rig.pump.step()
    rig.server.begin_elsewhere()
    rig.store.send("alice", ME, "after start")
    rig.pump.step()
    assert rig.server.methods()[-1] == "turn/steer"
    assert rig.server.sent[-1]["params"]["expectedTurnId"] == "turn-1"


def test_a_turn_end_race_refreshes_after_the_refusal_and_starts_a_turn(rig):
    rig.server.begin_elsewhere()
    rig.pump.start()
    rig.pump.step()
    assert rig.pump.state.active_turn == "turn-1"
    rig.server.finish(announce=False)
    item = rig.store.send("alice", ME, "racing")
    rig.pump.step()
    assert rig.server.methods()[-3:] == ["turn/steer", "thread/read", "turn/start"]
    [delivery] = deliveries(rig, item.id)
    assert rig.server.turns[1]["inputs"] == [
        f"[swarm delivery] item {item.id} delivery {delivery.id} from alice\n\nracing"
    ]
    assert rig.store.get(item.id).state == "delivered"
    assert rig.pump.state.active_turn == "turn-2"


def test_a_completed_event_makes_the_next_delivery_start_a_turn(rig):
    rig.server.begin_elsewhere()
    rig.pump.start()
    rig.pump.step()
    rig.server.finish()
    rig.store.send("alice", ME, "next")
    rig.pump.step()
    assert rig.server.methods()[-1] == "turn/start"
    assert "turn/steer" not in rig.server.methods()


def test_a_second_refusal_rejects_the_delivery_and_holds_the_pump(rig):
    rig.server.begin_elsewhere()
    rig.pump.start()
    rig.pump.step()
    rig.server.turn_start = lambda params: "thread is closing"
    rig.server.turn_steer = lambda params: "thread is closing"
    item = rig.store.send("alice", ME, "refused")
    rig.pump.step()
    [delivery] = deliveries(rig, item.id)
    assert (delivery.state, delivery.reason) == ("rejected", "turn/steer refused: thread is closing")
    assert rig.store.get(item.id).state == "pending"
    assert (rig.pump.hold_until, rig.pump.backoff) == (rig.clock.now + 1.0, 2.0)
    rig.pump.step()
    assert rig.server.methods().count("turn/steer") == 2


def test_a_refusal_releases_the_queued_successors_and_order_is_kept(rig, recovery_deadline):
    rig.pump.start()
    rig.pump.step()
    items = [rig.store.send("alice", ME, text) for text in ("one", "two", "three")]
    refuse = iter(["thread is closing", "thread is closing"])
    accept = rig.server.turn_start
    rig.server.turn_start = lambda params: next(refuse, None) or accept(params)
    rig.pump.step()
    assert [rig.store.get(item.id).state for item in items] == ["pending", "pending", "pending"]
    assert [d.state for item in items for d in deliveries(rig, item.id)] == ["rejected", "rejected", "rejected"]
    assert [d.reason for item in items[1:] for d in deliveries(rig, item.id)] == [RELEASED, RELEASED]
    assert rig.pump.queue == []
    assert rig.pump.next_reconcile == rig.pump.hold_until
    rig.clock.now = rig.pump.hold_until
    rig.pump.step()
    texts = [m["params"]["input"][0]["text"].rsplit("\n", 1)[1] for m in rig.server.sent if "input" in m["params"]]
    assert texts[-3:] == ["one", "two", "three"]
    assert [rig.store.get(item.id).state for item in items] == ["delivered", "delivered", "delivered"]


def test_a_failed_refresh_after_a_refusal_still_rejects_the_delivery(rig):
    rig.server.begin_elsewhere()
    rig.pump.start()
    rig.pump.step()
    rig.server.finish(announce=False)
    rig.server.silent.add("thread/read")
    item = rig.store.send("alice", ME, "refused then lost")
    rig.pump.step()
    [delivery] = deliveries(rig, item.id)
    assert (delivery.state, delivery.reason) == ("rejected", "turn/steer refused: expected turn mismatch")
    assert rig.store.get(item.id).state == "pending"


def test_a_steer_reply_for_a_turn_that_already_completed_leaves_the_thread_idle(rig):
    rig.pump.start()
    rig.pump.step()
    rig.server.begin_elsewhere()
    rig.pump.step()
    accept = rig.server.turn_steer

    def steer_then_complete(params):
        result = accept(params)
        rig.server.finish(announce=False)
        rig.server.early.append({"method": codex.TURN_COMPLETED, "params": {"turn": {"id": "turn-1"}}})
        return result

    rig.server.turn_steer = steer_then_complete
    rig.server.frames.clear()
    rig.store.send("alice", ME, "last words")
    rig.pump.step()
    assert rig.server.frames == []
    assert rig.pump.state.active_turn == ""
    rig.store.send("alice", ME, "after the end")
    rig.pump.step()
    assert rig.server.methods()[-1] == "turn/start"


def test_a_payload_that_changed_since_reservation_is_released_unsent(rig):
    item = rig.store.send("alice", ME, "original")
    rig.pump.start()
    rig.pump.queue += rig.pump.dispatcher.reserve(ME, OWNER)
    rig.store.redis.hset(rig.store.key("item", item.id), "text", "changed")
    rig.pump._submit(rig.pump.queue.pop(0))
    [delivery] = deliveries(rig, item.id)
    assert (delivery.state, delivery.reason) == ("rejected", MISMATCH)
    assert not [m for m in rig.server.methods() if m.startswith("turn/")]


def test_a_changed_payload_in_the_middle_keeps_inbox_order(rig, recovery_deadline):
    items = [rig.store.send("alice", ME, text) for text in ("one", "two", "three")]
    rig.pump.start()
    rig.pump.queue += rig.pump.dispatcher.reserve(ME, OWNER)
    rig.store.redis.hset(rig.store.key("item", items[1].id), "text", "two changed")
    rig.pump._submit(rig.pump.queue.pop(0))
    rig.pump._submit(rig.pump.queue.pop(0))
    assert rig.pump.queue == []
    assert [d.reason for item in items[1:] for d in deliveries(rig, item.id)] == [MISMATCH, RELEASED]
    assert rig.pump.next_reconcile == rig.clock.now
    rig.pump.step()
    texts = [m["params"]["input"][0]["text"].rsplit("\n", 1)[1] for m in rig.server.sent if "input" in m["params"]]
    assert texts == ["one", "two changed", "three"]
    assert [rig.store.get(item.id).state for item in items] == ["delivered", "delivered", "delivered"]


def test_a_reply_naming_the_completed_turn_keeps_a_newer_active_turn():
    state = codex.ThreadState("turn-2", ended="turn-1")
    state.follow("turn-1")
    assert state.active_turn == "turn-2"
    state.follow("turn-3")
    assert state.active_turn == "turn-3"
    idle = codex.ThreadState("", ended="turn-1")
    idle.follow("turn-1")
    assert idle.active_turn == ""


def test_a_turn_running_before_the_pump_connected_is_not_steered_and_holds_delivery_until_it_ends(rig):
    rig.server.begin_elsewhere()
    rig.server.frames.clear()
    rig.pump.start()
    item = rig.store.send("alice", ME, "careful")
    rig.pump.step()
    assert not [m for m in rig.server.methods() if m.startswith("turn/")]
    assert rig.record().pending_request == codex.UNOBSERVED
    assert rig.logged == [f"thread {THREAD} waits on {codex.UNOBSERVED}; inbox delivery is held"]
    rig.server.finish()
    rig.pump.step()
    assert rig.server.methods()[-1] == "turn/start"
    assert rig.store.get(item.id).state == "delivered"
    assert rig.record().pending_request == ""


def test_the_backoff_doubles_to_its_cap_and_resets_on_acceptance(rig):
    rig.pump.start()
    rig.pump.backoff = 40.0
    rig.pump._hold()
    assert rig.pump.backoff == rig.pump.timing.backoff_max_s
    item = rig.store.send("alice", ME, "ok")
    rig.clock.now = rig.pump.hold_until
    rig.server.frames.append({"method": "thread/status/changed", "params": {"threadId": THREAD}})
    rig.pump.step()
    assert rig.clock.now == rig.pump.hold_until
    assert rig.store.get(item.id).state == "delivered"
    assert rig.pump.backoff == 1.0


def test_a_fresh_pump_delivers_from_clock_zero(rig):
    rig.clock.now = 0.0
    item = rig.store.send("alice", ME, "early")
    rig.pump.start()
    rig.pump.step()
    assert rig.store.get(item.id).state == "delivered"


def test_an_unchanged_empty_request_set_writes_no_pending_mark(rig):
    marks = []
    original = rig.bindings.put
    rig.bindings.put = lambda slug, record: marks.append(record.pending_request) or original(slug, record)
    rig.pump.start()
    rig.pump.step()
    rig.pump.step()
    assert marks == ["", ""]
    assert rig.logged == []


def test_a_refused_ownership_at_start_degrades_without_a_subscription(rig):
    Dispatcher(rig.store).own(ME, "host:99")
    assert rig.run(1) == DEGRADED
    assert rig.pump.subscription is None
    assert rig.logged == [
        f"codex pump for {ME} degraded: DispatchError: {ME} is delivered by host:99; take over to replace it"
    ]
    assert (rig.record().state, rig.record().last_error_class) == (DEGRADED, "DispatchError")


def test_run_closes_its_subscription(rig):
    rig.run(1)
    closed = rig.pump.subscription
    assert closed.subscribed is False


def test_a_pending_approval_keeps_the_item_reserved_and_surfaces_the_request(rig):
    rig.server.begin_elsewhere()
    rig.server.frames.append({"id": 90, "method": APPROVAL, "params": {"threadId": THREAD}})
    rig.pump.start()
    rig.pump.step()
    rig.pump.step()
    item = rig.store.send("alice", ME, "wait for approval")
    rig.pump.step()
    [delivery] = deliveries(rig, item.id)
    assert delivery.state == "reserved"
    assert not [m for m in rig.server.methods() if m.startswith("turn/")]
    assert rig.record().pending_request == APPROVAL
    assert rig.logged == [f"thread {THREAD} waits on {APPROVAL}; inbox delivery is held"]
    assert 90 not in [m.get("id") for m in rig.server.sent if "result" in m]
    later = rig.store.send("bob", ME, "also waiting")
    rig.pump.step()
    assert [d.state for d in deliveries(rig, later.id)] == ["reserved"]
    rig.server.frames.append({"method": codex.RESOLVED, "params": {"threadId": THREAD, "requestId": 90}})
    rig.pump.step()
    assert rig.server.methods()[-2:] == ["turn/steer", "turn/steer"]
    assert [rig.store.get(i.id).state for i in (item, later)] == ["delivered", "delivered"]
    assert rig.record().pending_request == ""


def test_the_active_turn_completing_drops_its_open_requests():
    state = codex.ThreadState("turn-1", {7: APPROVAL})
    state.observe({"method": codex.TURN_COMPLETED, "params": {"turn": {"id": "turn-1"}}})
    assert (state.active_turn, state.requests) == ("", {})


def test_a_late_completion_of_an_earlier_turn_keeps_the_active_turn_and_its_requests():
    state = codex.ThreadState("turn-1", {7: APPROVAL})
    state.observe({"method": codex.TURN_COMPLETED, "params": {"turn": {"id": "turn-0"}}})
    assert (state.active_turn, state.requests) == ("turn-1", {7: APPROVAL})


def test_a_lost_notify_is_recovered_by_the_scan_and_not_before(rig, recovery_deadline):
    rig.pump.start()
    rig.pump.step()
    item = rig.store.send("alice", ME, "lost hint")
    rig.lose_notifies()
    rig.clock.now = rig.pump.next_reconcile - 1
    rig.pump.step()
    assert rig.store.get(item.id).state == "pending"
    assert "turn/start" not in rig.server.methods()
    rig.clock.now = rig.pump.next_reconcile
    rig.pump.step()
    assert rig.store.get(item.id).state == "delivered"


def test_a_notify_delivers_before_the_scan_is_due(rig, recovery_deadline):
    rig.pump.start()
    rig.pump.step()
    due = rig.pump.next_reconcile
    item = rig.store.send("alice", ME, "hinted")
    rig.pump.step()
    assert rig.clock.now < due
    assert rig.store.get(item.id).state == "delivered"


def test_hint_checks_never_block(rig):
    rig.pump.start()
    real = rig.pump.subscription
    timeouts = []

    class Spy:
        def get_message(self, timeout):
            timeouts.append(timeout)
            return real.get_message(timeout=0)

        def close(self):
            real.close()

    rig.pump.subscription = Spy()
    rig.store.send("alice", ME, "hint")
    rig.pump.step()
    rig.pump.step()
    assert len(timeouts) >= 3
    assert set(timeouts) == {0}


def test_items_go_one_at_a_time_in_inbox_order(rig, recovery_deadline):
    first = rig.store.send("alice", ME, "first")
    second = rig.store.send("bob", ME, "second")
    rig.run(1)
    starts = [m for m in rig.server.sent if m.get("method") in ("turn/start", "turn/steer")]
    assert [m["method"] for m in starts] == ["turn/start", "turn/steer"]
    assert [m["params"]["input"][0]["text"].rsplit("\n", 1)[1] for m in starts] == ["first", "second"]
    assert [rig.store.get(i.id).state for i in (first, second)] == ["delivered", "delivered"]


def test_a_lost_acknowledgement_stays_unknown_and_is_never_replayed(rig, recovery_deadline):
    rig.server.silent.add("turn/start")
    item = rig.store.send("alice", ME, "maybe")
    assert rig.run(1) == DEGRADED
    [delivery] = deliveries(rig, item.id)
    assert delivery.state == "unknown"
    assert (rig.record().state, rig.record().last_error_class) == (DEGRADED, "TimeoutError")
    rig.server.silent.clear()
    rig.pump = rig.new_pump()
    assert rig.run(2) == codex.STOPPED
    assert rig.server.methods().count("turn/start") == 1
    assert rig.store.get(item.id).state == "pending"
    assert [d.state for d in deliveries(rig, item.id)] == ["unknown"]


def test_a_disconnect_marks_the_delivery_unknown_and_degrades(rig):
    from websockets.exceptions import ConnectionClosedError

    rig.pump.start()
    rig.pump.step()
    item = rig.store.send("alice", ME, "cut")
    rig.server.broken = ConnectionClosedError(None, None)
    rig.pump.queue += rig.pump.dispatcher.reserve(ME, OWNER)
    with pytest.raises(protocol.ProtocolError):
        rig.pump._submit(rig.pump.queue.pop(0))
    assert [d.state for d in deliveries(rig, item.id)] == ["unknown"]
    assert rig.pump.queue == []


def test_a_broken_event_stream_degrades_the_transport(rig):
    from websockets.exceptions import ConnectionClosedError

    rig.server.broken = ConnectionClosedError(None, None)
    assert rig.run(3) == DEGRADED
    assert (rig.record().state, rig.record().last_error_class) == (DEGRADED, "ProtocolError")
    assert rig.logged == [
        f"codex pump for {ME} degraded: ProtocolError: thread/read lost the connection: ConnectionClosedError"
    ]


def test_a_stray_text_frame_is_refused_and_diagnostics_stay_off_the_socket(rig):
    rig.server.frames.append("codex: warning written to the wrong stream")
    assert rig.run(3) == DEGRADED
    assert rig.logged == [f"codex pump for {ME} degraded: ProtocolError: the event stream got a frame that is not JSON"]
    assert all(message["jsonrpc"] == "2.0" for message in rig.server.sent)
    assert rig.server.sent == [
        {"jsonrpc": "2.0", "id": 1, "method": "thread/read", "params": {"threadId": THREAD, "includeTurns": True}}
    ]


def test_losing_ownership_degrades_at_the_next_reconcile(rig):
    rig.pump.start()
    rig.pump.step()
    Dispatcher(rig.store).own(ME, "host:99", takeover=True)
    rig.clock.now = rig.pump.next_reconcile
    with pytest.raises(DispatchError) as lost:
        rig.pump.step()
    assert str(lost.value) == f"{OWNER} no longer delivers for {ME}"


def test_start_owns_the_recipient_and_releases_unsent_reservations(rig):
    rig.store.send("alice", ME, "left over")
    Dispatcher(rig.store).own(ME, OWNER)
    [stale] = Dispatcher(rig.store).reserve(ME, OWNER)
    codex.Transport(rig.bindings, SLUG, EXECUTION).mark(state=DEGRADED, last_error_class="ProtocolError")
    rig.pump.start()
    assert Dispatcher(rig.store).owner(ME) == OWNER
    assert Receipts(rig.store).get(stale.id).state == "rejected"
    assert (rig.record().state, rig.record().last_error_class) == (CONNECTED, "")


def test_reconcile_records_the_observed_turn_and_time(rig):
    rig.server.begin_elsewhere()
    rig.server.frames.clear()
    rig.pump.start()
    rig.pump.step()
    record = rig.record()
    assert (record.active_turn_id, record.last_reconcile_at) == ("turn-1", int(rig.clock.now * 1000))
    assert rig.pump.next_reconcile == rig.clock.now + 30


def test_a_withdrawn_item_is_released_instead_of_submitted(rig):
    item = rig.store.send("alice", ME, "withdrawn")
    rig.pump.start()
    rig.pump.queue += rig.pump.dispatcher.reserve(ME, OWNER)
    rig.store.redis.hset(rig.store.key("item", item.id), "state", "done")
    rig.pump._submit(rig.pump.queue.pop(0))
    [delivery] = deliveries(rig, item.id)
    assert (delivery.state, delivery.reason) == (
        "rejected",
        f"message {item.id} is done for {ME}, not {ME}'s to submit",
    )
    assert "turn/start" not in rig.server.methods()


def test_refresh_reads_the_last_turn_status():
    state = codex.ThreadState("old")
    state.refresh({"turns": [{"id": "a", "status": codex.IN_PROGRESS}, {"id": "b", "status": "completed"}]})
    assert state.active_turn == ""
    state.refresh({"turns": [{"id": "a", "status": "completed"}, {"id": "b", "status": codex.IN_PROGRESS}]})
    assert state.active_turn == "b"
    state.refresh({})
    assert state.active_turn == ""


def test_stderr_writes_one_prefixed_line(capsys):
    codex.stderr("degraded")
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "[agentihooks codex-pump] degraded\n")
