import pytest

from scripts.events.redis_state import RedisStateStore
from scripts.scheduler import commands
from scripts.scheduler.slots import BroadcastSlot, InboxSlot, SwarmSlot, restore

pytestmark = pytest.mark.unit

OWNER = "engineer@323133-1958"


@pytest.fixture
def scheduler():
    import fakeredis

    from scripts.scheduler.core import Scheduler

    return Scheduler(RedisStateStore(fakeredis.FakeRedis(decode_responses=True), "scheduler"), machine="proof")


@pytest.fixture
def swarm(scheduler):
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    store = RedisStore(scheduler.state.redis)
    store.create(SwarmConfig("sw", "/repo", 2, 1))
    for name, lane in (("sw-eng-1", "eng"), ("sw-eng-2", "eng"), ("sw-ci-1", "ci")):
        store.put_agent("sw", AgentRecord(name, lane, "t"))
    return store


def refused(message):
    return pytest.raises(commands.CommandError, match=f"^{message}$")


@pytest.mark.parametrize(
    ("target", "name", "slot"),
    [
        ("fleet", "", BroadcastSlot()),
        ("swarm", "sw", SwarmSlot("sw")),
        ("seat", "master@sw", InboxSlot("master@sw")),
        ("session", OWNER, InboxSlot(OWNER)),
    ],
)
def test_each_target_names_its_slot(target, name, slot):
    assert commands.target_slot(target, name) == slot


def test_fleet_is_the_target_without_a_name():
    assert commands.target_slot("fleet") == BroadcastSlot(None)


@pytest.mark.parametrize(
    ("target", "name", "message"),
    [
        ("pane", "x", "target must be one of fleet, swarm, seat, session, not pane"),
        ("fleet", "sw", "the fleet target takes no name"),
        ("swarm", "", "the swarm target needs a name"),
        ("seat", "", "the seat target needs a name"),
        ("session", "", "the session target needs a name"),
        ("seat", OWNER, f"{OWNER} is not a seat address"),
        ("session", "master@sw", "master@sw is not a session address"),
    ],
)
def test_a_target_that_does_not_fit_is_refused(target, name, message):
    with refused(message):
        commands.target_slot(target, name)


@pytest.mark.parametrize(
    ("text", "value"),
    [("90", 90.0), ("90s", 90.0), ("5m", 300.0), ("2h", 7200.0), ("1d", 86400.0), ("1.5m", 90.0), (" 5m ", 300.0)],
)
def test_durations_read_seconds_and_units(text, value):
    assert commands.seconds(text) == value


@pytest.mark.parametrize("text", ["5x", "", "m", "-5", "5 m"])
def test_a_duration_without_a_number_and_unit_is_refused(text):
    with refused(f"{text} is not a duration such as 90, 90s, 5m or 2h"):
        commands.seconds(text)


def test_moments_read_iso_time():
    assert commands.moment(" 2026-10-18T09:30:00+00:00 ") == 1792315800.0


@pytest.mark.parametrize("text", ["soon", "2026-10-18T09:30:00"])
def test_a_moment_that_is_not_iso_with_an_offset_is_refused(text):
    with refused(f"{text} is not an ISO time with an offset such as 2026-10-18T09:30:00\\+00:00"):
        commands.moment(text)


def test_a_timer_after_a_delay_is_armed_for_its_target(scheduler):
    view = commands.add_timer(scheduler, OWNER, BroadcastSlot(), "hello", delay=60, now=100)
    assert view == {
        "id": view["id"],
        "kind": "timer",
        "state": "active",
        "owner": OWNER,
        "payload": "hello",
        "due": 160,
        "targets": [{"kind": "broadcast", "channel": None}],
    }
    assert len(view["id"]) == 32
    assert scheduler.get(view["id"]).record["slots"][0]["signal"] == "timeout"


def test_a_timer_at_a_time_is_due_then(scheduler):
    view = commands.add_timer(scheduler, OWNER, InboxSlot("master@sw"), "at five", at=500, now=100)
    assert (view["due"], view["targets"]) == (500, [{"kind": "inbox", "address": "master@sw"}])


@pytest.mark.parametrize(
    ("message", "when", "refusal"),
    [
        ("hello", {"delay": -1}, "delay must not be negative"),
        (" ", {"delay": 5}, "a schedule needs an owner and payload"),
        ("hello", {}, "a timer needs either delay or at"),
    ],
)
def test_a_timer_the_scheduler_refuses_is_a_command_refusal(scheduler, message, when, refusal):
    with refused(refusal):
        commands.add_timer(scheduler, OWNER, BroadcastSlot(), message, now=100, **when)


def test_a_stopwatch_fires_its_limit_signal(scheduler, swarm):
    view = commands.start_stopwatch(scheduler, OWNER, SwarmSlot("sw"), "too long", limit=30, now=100)
    assert view == {
        "id": view["id"],
        "kind": "stopwatch",
        "state": "active",
        "owner": OWNER,
        "payload": "too long",
        "due": 130,
        "targets": [{"kind": "swarm", "slug": "sw"}],
        "limit": 30,
    }
    assert scheduler.get(view["id"]).record["slots"][0]["signal"] == "limit_reached"


def test_a_negative_stopwatch_limit_is_refused(scheduler):
    with refused("limit must not be negative"):
        commands.start_stopwatch(scheduler, OWNER, BroadcastSlot(), "x", limit=-1, now=100)


def test_a_cron_is_due_at_its_next_match_in_utc_by_default(scheduler):
    view = commands.add_cron(scheduler, OWNER, BroadcastSlot("ops"), "tick", expression="*/5 * * * *", now=1)
    assert view == {
        "id": view["id"],
        "kind": "cron",
        "state": "active",
        "owner": OWNER,
        "payload": "tick",
        "due": 300,
        "targets": [{"kind": "broadcast", "channel": "ops"}],
        "expression": "*/5 * * * *",
        "timezone": "UTC",
    }


def test_a_cron_reads_its_expression_in_its_time_zone(scheduler):
    view = commands.add_cron(
        scheduler, OWNER, BroadcastSlot(), "nine", expression="0 9 * * *", timezone="Europe/Madrid", now=0
    )
    assert (view["due"], view["timezone"]) == (8 * 3600, "Europe/Madrid")


@pytest.mark.parametrize(
    ("expression", "timezone", "message"),
    [
        ("* * *", "UTC", "a cron expression needs five fields"),
        ("99 * * * *", "UTC", "\\[99 \\* \\* \\* \\*\\] is not acceptable, out of range"),
        ("* * * * *", "Mars/Base", "Mars/Base is not a time zone"),
        ("* * * * *", "", " is not a time zone"),
    ],
)
def test_a_cron_the_scheduler_refuses_is_a_command_refusal(scheduler, expression, timezone, message):
    with refused(message):
        commands.add_cron(scheduler, OWNER, BroadcastSlot(), "x", expression=expression, timezone=timezone, now=0)
    assert scheduler.document()["entries"] == {}


def test_listing_shows_open_entries_of_one_kind_soonest_first(scheduler):
    late = commands.add_timer(scheduler, OWNER, InboxSlot(OWNER), "late", delay=90, now=100)
    soon = commands.add_timer(scheduler, OWNER, InboxSlot(OWNER), "soon", delay=10, now=100)
    ready = scheduler.timer(owner=OWNER, payload="ready", delay=50, now=100)
    gone = commands.add_timer(scheduler, OWNER, InboxSlot(OWNER), "gone", delay=5, now=100)
    commands.cancel(scheduler, "timer", gone["id"])
    commands.start_stopwatch(scheduler, OWNER, InboxSlot(OWNER), "watch", limit=1, now=100)
    commands.add_cron(scheduler, OWNER, InboxSlot(OWNER), "cron", expression="* * * * *", now=100)
    assert [view["id"] for view in commands.entries(scheduler, "timer")] == [soon["id"], ready.id, late["id"]]
    assert [view["payload"] for view in commands.entries(scheduler, "cron")] == ["cron"]


def test_a_timer_whose_delivery_is_in_flight_stays_listed(scheduler, monkeypatch):
    soon = commands.add_timer(scheduler, OWNER, InboxSlot(OWNER), "soon", delay=10, now=100)
    late = commands.add_timer(scheduler, OWNER, InboxSlot(OWNER), "late", delay=90, now=100)

    def lost(self, event, delivery_id, redis):
        raise RuntimeError("delivery lost")

    monkeypatch.setattr(InboxSlot, "deliver", lost)
    assert scheduler.run_due(now=120) == 1
    assert [(view["id"], view["state"]) for view in commands.entries(scheduler, "timer")] == [
        (soon["id"], "pending"),
        (late["id"], "active"),
    ]
    assert commands.cancel(scheduler, "timer", soon["id"])["state"] == "cancelled"


def test_a_fired_timer_leaves_the_list(scheduler):
    commands.add_timer(scheduler, OWNER, InboxSlot(OWNER), "fire", delay=1, now=100)
    scheduler.run_due(now=101)
    assert commands.entries(scheduler, "timer") == []


def test_cancelling_ends_an_open_entry_once(scheduler):
    timer = commands.add_timer(scheduler, OWNER, BroadcastSlot(), "x", delay=5, now=100)
    assert commands.cancel(scheduler, "timer", timer["id"])["state"] == "cancelled"
    with refused(f"timer {timer['id']} is already cancelled"):
        commands.cancel(scheduler, "timer", timer["id"])


def test_cancelling_names_only_entries_of_its_kind(scheduler):
    cron = commands.add_cron(scheduler, OWNER, BroadcastSlot(), "x", expression="* * * * *", now=0)
    with refused("no timer nope"):
        commands.cancel(scheduler, "timer", "nope")
    with refused(f"no timer {cron['id']}"):
        commands.cancel(scheduler, "timer", cron["id"])
    assert commands.cancel(scheduler, "cron", cron["id"])["state"] == "cancelled"


def test_a_stopwatch_shows_and_stops_with_its_elapsed_time(scheduler):
    watch = commands.start_stopwatch(scheduler, OWNER, BroadcastSlot(), "x", limit=60, now=100)
    shown = commands.show_stopwatch(scheduler, watch["id"], now=110)
    assert (shown["state"], shown["elapsed"], shown["limit"]) == ("active", 10, 60)
    stopped = commands.stop_stopwatch(scheduler, watch["id"], now=130)
    assert (stopped["state"], stopped["elapsed"]) == ("stopped", 30)
    assert commands.show_stopwatch(scheduler, watch["id"], now=999)["elapsed"] == 30
    with refused(f"stopwatch {watch['id']} is already stopped"):
        commands.stop_stopwatch(scheduler, watch["id"], now=140)


def test_showing_names_only_stopwatches(scheduler):
    timer = commands.add_timer(scheduler, OWNER, BroadcastSlot(), "x", delay=5, now=100)
    with refused("no stopwatch nope"):
        commands.show_stopwatch(scheduler, "nope")
    with refused(f"no stopwatch {timer['id']}"):
        commands.show_stopwatch(scheduler, timer["id"])
    with refused(f"no stopwatch {timer['id']}"):
        commands.stop_stopwatch(scheduler, timer["id"])


def test_a_schedule_for_a_swarm_that_does_not_exist_is_refused(scheduler, swarm):
    for add in (
        lambda: commands.add_timer(scheduler, OWNER, SwarmSlot("nope"), "x", delay=5, now=100),
        lambda: commands.start_stopwatch(scheduler, OWNER, SwarmSlot("nope"), "x", limit=5, now=100),
        lambda: commands.add_cron(scheduler, OWNER, SwarmSlot("nope"), "x", expression="* * * * *", now=100),
    ):
        with refused("no swarm nope"):
            add()
    assert scheduler.records() == []
    assert commands.add_timer(scheduler, OWNER, SwarmSlot("sw"), "x", delay=5, now=100)["state"] == "active"


def test_a_schedule_that_cannot_start_is_cancelled_not_left_ready(scheduler, monkeypatch):
    from scripts.scheduler.core import Entry

    def broken(self, *, now=None):
        raise RuntimeError("redis went away")

    monkeypatch.setattr(Entry, "start", broken)
    with pytest.raises(RuntimeError, match="^redis went away$"):
        commands.add_timer(scheduler, OWNER, BroadcastSlot(), "x", delay=5, now=100)
    assert [record["state"] for record in scheduler.records()] == ["cancelled"]
    assert commands.entries(scheduler, "timer") == []


def test_a_swarm_target_reaches_each_live_agent_once_but_not_the_owner(scheduler, swarm):
    from scripts.inbox.outbox import stable_id
    from scripts.inbox.store import InboxStore

    event = {"owner": "sw-eng-1", "payload": "stand up"}
    SwarmSlot("sw").deliver(event, "delivery-1", scheduler.state)
    SwarmSlot("sw").deliver(event, "delivery-1", scheduler.state)
    inbox = InboxStore(scheduler.state.redis)
    assert inbox.inbox("sw-eng-1") == []
    for name in ("sw-eng-2", "sw-ci-1"):
        items = inbox.inbox(name)
        assert [(i.id, i.sender, i.text, i.task) for i in items] == [
            (stable_id("delivery-1", name), "sw-eng-1", "stand up", "t")
        ]


def test_a_swarm_slot_survives_its_record():
    assert SwarmSlot("sw").record() == {"kind": "swarm", "slug": "sw"}
    assert restore({"kind": "swarm", "slug": "sw"}) == SwarmSlot("sw")


def test_connect_starts_the_runner_on_the_shared_redis(monkeypatch, scheduler):
    from scripts.scheduler import service
    from scripts.swarm import store

    started = []
    monkeypatch.setattr(store, "redis_client", lambda: scheduler.state.redis)
    monkeypatch.setattr(service, "ensure_running", lambda: started.append(True))
    connected = commands.connect()
    assert connected.state.redis is scheduler.state.redis
    assert started == [True]
