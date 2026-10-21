import json
import os

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import cli, lease, scheduled_tick, tick_heartbeat, tick_watch
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr

pytestmark = pytest.mark.unit
MINUTE = 60_000
LIMIT = tick_heartbeat.LIMIT_MS
START = 1_800_000_000_000
FRONT_DOOR = "master@sw"


@pytest.fixture
def store():
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    return found


def capacity(store, observed_at, accounts=()):
    decision = {"observed_at": observed_at, "accounts": [{"observed_at": at} for at in accounts]}
    store.redis.set(store.key("sw", "quota-capacity"), json.dumps(decision))


def beat(store, at, rev=1, host=None, quota=None):
    capacity(store, at if host is None else host, [(at if quota is None else quota) / 1000])
    return tick_heartbeat.write(store, "sw", at, {"_meta": {"rev": rev}})


def agent(store, name, seat, lane="eng", state="working"):
    store.put_agent("sw", AgentRecord(name, lane, "t1" if lane != MASTER else MASTER, state=state, seat=seat))


def mail(store, address):
    return [(item.sender, item.text, item.fyi) for item in InboxStore(store.redis).inbox(address)]


def texts(store, address):
    return [text for _, text, _ in mail(store, address)]


def bounded(passes):
    calls = []

    def wait(seconds):
        calls.append(seconds)
        assert len(calls) <= passes, "the watcher ran past its passes"

    return wait


def test_a_pass_records_when_it_ended_and_when_each_input_it_decided_on_was_observed(store):
    found = beat(store, START, rev=7, host=START - 2_000, quota=START - 30_000)

    assert found == tick_heartbeat.read(store, "sw")
    assert found.at == START
    assert found.rev == 7
    assert found.observed == {"host room": START - 2_000, "quota": START - 30_000, "ledger revision": START}


def test_the_quota_input_is_its_freshest_account_observation(store):
    capacity(store, START, [(START - 9 * MINUTE) / 1000, (START - MINUTE) / 1000])

    assert tick_heartbeat.write(store, "sw", START, {"_meta": {"rev": 1}}).observed["quota"] == START - MINUTE


def test_an_input_never_observed_is_recorded_as_unknown_and_never_judged_frozen(store):
    found = tick_heartbeat.write(store, "sw", START, {})

    assert found.observed == {"host room": None, "quota": None, "ledger revision": None}
    assert tick_heartbeat.stale(found, START + MINUTE) == []


def test_the_ledger_revision_keeps_the_time_it_last_changed(store):
    beat(store, START, rev=3)
    held = beat(store, START + 4 * MINUTE, rev=3)
    moved = beat(store, START + 5 * MINUTE, rev=4)

    assert held.observed["ledger revision"] == START
    assert moved.observed["ledger revision"] == START + 5 * MINUTE


def test_a_ledger_revision_held_ten_minutes_is_a_frozen_input(store):
    beat(store, START, rev=3)
    held = beat(store, START + LIMIT, rev=3)

    assert tick_heartbeat.stale(held, START + LIMIT) == [
        "the ledger revision it decides on was 10 minutes old at its last pass"
    ]


def test_a_fresh_heartbeat_is_not_stale(store):
    found = beat(store, START)

    assert tick_heartbeat.stale(found, START + LIMIT - 1) == []


def test_ten_minutes_without_a_completed_pass_is_stale_with_the_last_pass_time(store):
    found = beat(store, START)

    assert tick_heartbeat.stale(found, START + LIMIT) == [
        f"no tick pass has completed for 10 minutes, the last ended at {tick_heartbeat.clock(START)}"
    ]


def test_a_decision_input_frozen_ten_minutes_is_stale_even_while_passes_complete(store):
    beat(store, START, rev=5)
    found = beat(store, START + LIMIT, rev=6, quota=START - MINUTE)

    assert tick_heartbeat.stale(found, START + LIMIT) == ["the quota it decides on was 11 minutes old at its last pass"]


def test_a_restarted_judge_gives_a_frozen_input_ten_minutes_from_its_own_start(store):
    found = beat(store, START, quota=START - 3 * LIMIT)

    assert tick_heartbeat.stale(found, START, START - LIMIT + 1) == []
    assert tick_heartbeat.stale(found, START, START - LIMIT) == [
        "the quota it decides on was 30 minutes old at its last pass"
    ]


def test_a_judge_that_started_after_the_last_pass_waits_ten_minutes_from_its_own_start(store):
    found = beat(store, START)
    since = START + 30 * MINUTE

    assert tick_heartbeat.stale(found, since + LIMIT - 1, since) == []
    assert tick_heartbeat.stale(found, since + LIMIT, since) == [
        f"no tick pass has completed for 40 minutes, the last ended at {tick_heartbeat.clock(START)}"
    ]


def test_a_missing_heartbeat_is_judged_only_from_a_given_start(store):
    assert tick_heartbeat.stale(None, START + LIMIT) == []
    assert tick_heartbeat.stale(None, START + LIMIT - 1, START) == []
    assert tick_heartbeat.stale(None, START + LIMIT, START) == [
        "no tick pass has completed in the 10 minutes since the watch began"
    ]


def test_every_completed_tick_pass_writes_the_heartbeat(env, monkeypatch):  # noqa: F811
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    clock = [START]
    monkeypatch.setattr(cli, "now_ms", lambda: clock[0])

    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    first, rev = tick_heartbeat.read(store, "sw"), ledger.state("sw")["_meta"]["rev"]
    clock[0] += MINUTE
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))

    assert first.at == START
    assert first.rev == rev
    assert tick_heartbeat.read(store, "sw").at == START + MINUTE


def test_a_pass_that_reads_the_ledger_clears_an_earlier_ledger_gone_mark(env):  # noqa: F811
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    store.redis.set(store.key("sw", "ledger-gone"), 1)

    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))

    assert not store.redis.exists(store.key("sw", "ledger-gone"))


def test_a_stale_tick_alerts_the_front_door_and_every_live_agent_seat_with_the_evidence(store):
    agent(store, "engineer@a1b2c3-0001", "eng-1@sw")
    agent(store, "ci@a1b2c3-0001", "ci-1@sw", lane="ci")
    agent(store, "engineer@a1b2c3-0002", "eng-2@sw", state="finished")
    beat(store, START)

    actions = tick_watch.watch_pass(store, "sw", START + LIMIT, START)

    evidence = f"no tick pass has completed for 10 minutes, the last ended at {tick_heartbeat.clock(START)}"
    alert = tick_watch.ALERT.format(slug="sw", evidence=evidence)
    for address in (FRONT_DOOR, "eng-1@sw", "ci-1@sw"):
        assert mail(store, address) == [("swarm", alert, True)]
    assert mail(store, "eng-2@sw") == []
    assert actions == [f"told {address} the tick is stale" for address in ("ci-1@sw", "eng-1@sw", FRONT_DOOR)]


def test_one_alert_per_stale_episode_however_many_passes_see_it(store):
    agent(store, "engineer@a1b2c3-0001", "eng-1@sw")
    beat(store, START)
    for at in range(START + LIMIT, START + 3 * LIMIT, MINUTE):
        tick_watch.watch_pass(store, "sw", at, START)

    assert len(mail(store, FRONT_DOOR)) == 1
    assert len(mail(store, "eng-1@sw")) == 1


def test_a_seat_that_comes_live_during_the_episode_gets_the_current_evidence(store):
    beat(store, START)
    tick_watch.watch_pass(store, "sw", START + LIMIT, START)
    agent(store, "engineer@a1b2c3-0001", "eng-1@sw")

    assert tick_watch.watch_pass(store, "sw", START + LIMIT + MINUTE, START) == ["told eng-1@sw the tick is stale"]
    evidence = f"no tick pass has completed for 11 minutes, the last ended at {tick_heartbeat.clock(START)}"
    assert texts(store, "eng-1@sw") == [tick_watch.ALERT.format(slug="sw", evidence=evidence)]
    assert len(texts(store, FRONT_DOOR)) == 1


@pytest.mark.parametrize("mark", tick_heartbeat.HALTS)
def test_a_halted_ledger_is_alerted_but_never_repaired(store, mark):
    beat(store, START - 3 * LIMIT)
    store.redis.set(store.key("sw", mark), 1)
    asked = []

    assert tick_watch.watch_pass(store, "sw", START, START, lambda slug, now: asked.append(slug) or []) == [
        f"told {FRONT_DOOR} the tick is stale"
    ]
    assert asked == []


def test_a_binned_ledger_marks_the_swarm_halted_and_a_later_read_clears_it(env):  # noqa: F811
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    ledger.binned = lambda slug: True
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert tick_heartbeat.halted(store, "sw")

    ledger.binned = lambda slug: False
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert not tick_heartbeat.halted(store, "sw")


def test_a_fresh_pass_sends_the_resolved_notice_to_every_alerted_seat_once(store):
    agent(store, "engineer@a1b2c3-0001", "eng-1@sw")
    beat(store, START)
    tick_watch.watch_pass(store, "sw", START + LIMIT, START)
    beat(store, START + LIMIT + 2 * MINUTE, rev=2)

    actions = tick_watch.watch_pass(store, "sw", START + LIMIT + 2 * MINUTE, START)
    again = tick_watch.watch_pass(store, "sw", START + LIMIT + 3 * MINUTE, START)

    resolved = tick_watch.RESOLVED.format(
        slug="sw", at=tick_heartbeat.clock(START + LIMIT + 2 * MINUTE), since=tick_heartbeat.clock(START + LIMIT)
    )
    for address in (FRONT_DOOR, "eng-1@sw"):
        assert texts(store, address)[1:] == [resolved]
    assert actions == [f"told {address} the tick runs again" for address in ("eng-1@sw", FRONT_DOOR)]
    assert again == []


def test_a_new_stale_episode_alerts_again(store):
    beat(store, START)
    tick_watch.watch_pass(store, "sw", START + LIMIT, START)
    beat(store, START + LIMIT + MINUTE, rev=2)
    tick_watch.watch_pass(store, "sw", START + LIMIT + MINUTE, START)
    tick_watch.watch_pass(store, "sw", START + 2 * LIMIT + MINUTE, START)

    assert len(texts(store, FRONT_DOOR)) == 3
    assert texts(store, FRONT_DOOR)[2].startswith("The tick of swarm sw is stale: ")


def test_a_stopped_swarm_is_not_watched(store):
    beat(store, START)
    store.update("sw", state="stopped")

    assert tick_watch.watch_pass(store, "sw", START + LIMIT, START) == []
    assert mail(store, FRONT_DOOR) == []


def test_a_swarm_stopped_mid_episode_ends_the_episode_with_a_closing_notice(store):
    beat(store, START)
    tick_watch.watch_pass(store, "sw", START + LIMIT, START)
    store.update("sw", state="stopped")

    actions = tick_watch.watch_pass(store, "sw", START + LIMIT + MINUTE, START)
    again = tick_watch.watch_pass(store, "sw", START + LIMIT + 2 * MINUTE, START)

    assert actions == [f"told {FRONT_DOOR} the tick is no longer watched"]
    assert texts(store, FRONT_DOOR)[-1] == tick_watch.UNWATCHED.format(
        slug="sw", at=tick_heartbeat.clock(START + LIMIT + MINUTE), since=tick_heartbeat.clock(START + LIMIT)
    )
    assert again == []


def test_the_watcher_respawns_the_master_only_while_the_tick_is_stale(store):
    asked = []
    beat(store, START)

    tick_watch.watch_pass(store, "sw", START + MINUTE, START, lambda slug, now: asked.append(slug) or [])
    assert asked == []
    assert tick_watch.watch_pass(store, "sw", START + LIMIT, START, lambda slug, now: [f"spawned master for {slug}"])[
        -1
    ] == ("spawned master for sw")


def test_the_watcher_run_passes_over_every_swarm_once_a_minute(store, capsys):
    store.create(SwarmConfig("other", "/repo", max_eng=1, max_ci=0))
    clock = [START]
    waits = []

    def wait(seconds):
        waits.append(seconds)
        assert len(waits) <= 2, "the watcher ran past its passes"
        clock[0] += LIMIT

    tick_watch.run(store, lambda: clock[0], None, wait, passes=2)

    assert waits == [tick_watch.PASS_S, tick_watch.PASS_S]
    assert "sw: told master@sw the tick is stale" in capsys.readouterr().out


def test_the_watch_command_runs_without_a_parsed_swarm(monkeypatch):
    ran = []
    monkeypatch.setattr(cli, "connect", lambda: "store")
    monkeypatch.setattr(cli, "refusal", lambda name, who: "")
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)
    monkeypatch.setattr(cli, "cmd_tick_watch", lambda store, args: ran.append((store, vars(args))))

    assert cli.dispatch(["tick-watch"]) == 0
    assert ran == [("store", {})]


def test_a_pass_that_ended_at_the_epoch_is_stale_a_full_limit_later():
    found = tick_heartbeat.Heartbeat(0, {})

    assert tick_heartbeat.stale(found, LIMIT) == [
        "no tick pass has completed for 10 minutes, the last ended at 00:00 UTC"
    ]
    assert tick_heartbeat.stale(tick_heartbeat.Heartbeat(LIMIT, {"quota": 0}), LIMIT) == [
        "the quota it decides on was 10 minutes old at its last pass"
    ]


def test_every_capacity_pass_refreshes_when_the_host_room_was_observed():
    from scripts.swarm import capacity
    from tests.swarm.test_capacity import _store, account
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    found = _store()
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    found.create(config)
    runtime = FakeRuntime()
    runtime.quota_capacity = lambda cfg, agents, now, demand, requirements: capacity.calculate(
        cfg, [account()], agents, demand
    )
    ledger = FakeLedger([{"id": "e"}])
    capacity.apply("sw", config, found, ledger, runtime, 1000)
    capacity.apply("sw", config, found, ledger, runtime, 2000)

    assert (capacity.read(found, "sw")["at"], capacity.read(found, "sw")["observed_at"]) == (1000, 2000)


def test_respawn_master_spawns_a_master_into_an_empty_front_door_seat(store, monkeypatch):
    from scripts.swarm import controller, tick
    from tests.swarm.test_tick import FakeRuntime

    monkeypatch.setenv("SWARM_HIVE_ID", "home")
    runtime = FakeRuntime()
    agent(store, "master@a1b2c3-0001", FRONT_DOOR, lane=MASTER)

    taken, take = [], controller.take_tick_lock
    monkeypatch.setattr(controller, "take_tick_lock", lambda *args: taken.append(args[-1]) or take(*args))

    actions = tick.respawn_master("sw", store, runtime, START)

    assert taken[:1] == [lease.ttl_ms()]
    assert not store.redis.exists(store.key("sw", "tick-lock"))
    assert lease.current(store, "sw").owner == "home"
    assert actions[0] == "dropped the gone master master@a1b2c3-0001"
    assert len(runtime.masters) == 1
    assert actions[-1].startswith("spawned master ")


def test_respawn_master_leaves_a_swarm_another_hive_ticks(store, monkeypatch):
    from scripts.swarm import tick
    from tests.swarm.test_tick import FakeRuntime

    monkeypatch.setenv("SWARM_HIVE_ID", "home")
    assert lease.acquire(store, "sw", "anton") is not None
    runtime = FakeRuntime()

    assert tick.respawn_master("sw", store, runtime, START) == []
    assert runtime.masters == []


def test_a_swarm_in_a_ledger_outage_is_alerted_but_never_repaired(store, tmp_path, monkeypatch):
    from scripts.swarm import outage

    monkeypatch.setattr(outage, "HOME", tmp_path)
    beat(store, START - 3 * LIMIT)
    outage.ledger_health("sw", START - 2 * LIMIT, lambda: False)
    asked = []

    assert tick_watch.watch_pass(store, "sw", START, START, lambda slug, now: asked.append(slug) or []) == [
        f"told {FRONT_DOOR} the tick is stale"
    ]
    assert asked == []


def test_a_restarted_watcher_alerts_at_once_on_a_heartbeat_already_ten_minutes_old(store):
    beat(store, START)

    assert tick_watch.watch_pass(store, "sw", START + LIMIT, START + LIMIT) == [f"told {FRONT_DOOR} the tick is stale"]


def test_times_and_durations_read_in_plain_words():
    assert tick_heartbeat.clock(3_900_000) == "01:05 UTC"
    assert [tick_heartbeat._minutes(ms) for ms in (60_000, 120_000, 659_999)] == ["1 minute", "2 minutes", "10 minutes"]


def test_alert_and_resolved_items_carry_stable_ids_per_seat_and_episode(store):
    beat(store, START)
    tick_watch.watch_pass(store, "sw", START + LIMIT, START)
    beat(store, START + LIMIT + MINUTE, rev=2)
    tick_watch.watch_pass(store, "sw", START + LIMIT + MINUTE, START)

    ids = [item.id for item in InboxStore(store.redis).inbox(FRONT_DOOR)]
    since = START + LIMIT
    assert ids == [
        tick_watch._item_id("sw", since, FRONT_DOOR, "alert"),
        tick_watch._item_id("sw", since, FRONT_DOOR, "resolved"),
    ]


def test_the_watcher_reports_a_failing_swarm_and_goes_on(store, monkeypatch, capsys):
    store.create(SwarmConfig("other", "/repo", max_eng=1, max_ci=0))
    real = tick_watch.watch_pass

    def failing(store, slug, *args):
        if slug == "other":
            raise RuntimeError("redis went away")
        return real(store, slug, *args)

    monkeypatch.setattr(tick_watch, "watch_pass", failing)
    beat(store, START - LIMIT)
    tick_watch.run(store, lambda: START, None, bounded(1), passes=1)

    out = capsys.readouterr()
    assert "other: RuntimeError: redis went away" in out.err
    assert f"sw: told {FRONT_DOOR} the tick is stale" in out.out


@pytest.mark.parametrize(
    ("deployment", "hub", "respawns"),
    [
        (None, "", True),
        ("local", "", True),
        ("distributed", "", False),
        (None, "https://hub.example:8765", False),
        ("local", "https://hub.example:8765", True),
    ],
)
def test_the_watch_command_respawns_only_on_a_workstation(monkeypatch, deployment, hub, respawns):
    from scripts.swarm import tick

    monkeypatch.setenv("LEDGER_URL", hub)
    if deployment is None:
        monkeypatch.delenv("AGENTIHOOKS_DEPLOYMENT", raising=False)
    else:
        monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", deployment)
    runtime, ran, asked = object(), [], []
    routed_with = []
    monkeypatch.setattr(cli, "routed", lambda herdr: routed_with.append(herdr) or runtime)
    monkeypatch.setattr(cli, "HerdrRuntime", lambda: "herdr")
    monkeypatch.setattr(cli, "now_ms", lambda: START)
    monkeypatch.setattr(tick, "respawn_master", lambda *args: asked.append(args) or ["spawned"])
    monkeypatch.setattr(tick_watch, "run", lambda *args: ran.append(args))

    cli.cmd_tick_watch("store", None)

    (args,) = ran
    assert args[:2] == ("store", cli.now_ms)
    if respawns:
        assert routed_with == ["herdr"]
        assert args[2].restart is scheduled_tick.restart
        assert args[2].respawn("sw") == ["spawned"]
        assert asked == [("sw", "store", runtime, START)]
    else:
        assert args[2] is None


def test_the_repair_restarts_the_scheduler_once_per_stale_window_and_respawns_every_pass():
    restarts, respawns = [], []
    repair = tick_watch.Repair(lambda: restarts.append(1) or "restarted", lambda slug: respawns.append(slug) or [])

    assert repair("sw", START) == ["restarted"]
    assert repair("other", START + LIMIT - 1) == []
    assert repair("sw", START + LIMIT) == ["restarted"]
    assert (len(restarts), respawns) == (2, ["sw", "other", "sw"])


def test_the_scheduler_restart_stops_the_running_supervisor_then_registers_the_tick_again():
    from contextlib import ExitStack

    from scripts.scheduler.service import machine_lock

    held, kills = ExitStack(), []
    held.enter_context(machine_lock("supervisor"))

    def kill(pid, sig):
        kills.append((pid, sig))
        held.close()

    assert scheduled_tick.restart(kill, lambda: True, lambda seconds: None) == "restarted the scheduler tick"
    assert kills == [(os.getpid(), scheduled_tick.signal.SIGTERM)]


def test_the_scheduler_restart_with_no_supervisor_only_registers_the_tick():
    kills, waits = [], []

    said = scheduled_tick.restart(lambda *args: kills.append(args), lambda: False, waits.append)

    assert said == "restarted the scheduler, but the tick did not register"
    assert (kills, waits) == ([], [])


def test_the_scheduler_restart_waits_for_the_supervisor_to_let_go():
    from contextlib import ExitStack

    from scripts.scheduler.service import machine_lock

    held, waits = ExitStack(), []
    held.enter_context(machine_lock("supervisor"))

    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            held.close()

    scheduled_tick.restart(lambda pid, sig: None, lambda: True, wait)

    assert waits == [1, 1, 1]


def test_an_item_id_is_twelve_hex_characters():
    found = tick_watch._item_id("sw", START, FRONT_DOOR, "alert")

    assert len(found) == 12
    assert int(found, 16) >= 0


def test_the_watcher_run_hands_its_repair_to_every_stale_swarm(store):
    beat(store, START - LIMIT)
    asked = []

    tick_watch.run(store, lambda: START, lambda slug, now: asked.append((slug, now)) or [], bounded(1), passes=1)

    assert asked == [("sw", START)]


def test_an_alert_joins_every_piece_of_evidence(store):
    beat(store, START - LIMIT, quota=START - 3 * LIMIT)

    tick_watch.watch_pass(store, "sw", START, START)

    evidence = tick_heartbeat.stale(tick_heartbeat.read(store, "sw"), START)
    assert len(evidence) == 2
    assert texts(store, FRONT_DOOR) == [tick_watch.ALERT.format(slug="sw", evidence="; ".join(evidence))]


def test_the_scheduler_restart_registers_through_the_swarm_timer_by_default(monkeypatch):
    from scripts.swarm import timer

    monkeypatch.setattr(timer, "schedule", lambda: True)

    assert scheduled_tick.restart(lambda *args: None, None, lambda s: None) == "restarted the scheduler tick"


def test_respawn_master_adopts_a_live_master_of_this_swarm(store, monkeypatch):
    from scripts.swarm import tick
    from tests.swarm.test_tick import FakeRuntime

    monkeypatch.setenv("SWARM_HIVE_ID", "home")
    name = f"master@{store.ensure_code('sw').code}-0007"
    runtime = FakeRuntime()
    runtime.live.add(name)

    assert tick.respawn_master("sw", store, runtime, START) == [f"adopted live master {name}"]
    assert runtime.masters == []
    assert [a.started_at for a in store.agents("sw") if a.name == name] == [START]


@pytest.mark.parametrize("state", ["stopped"])
def test_respawn_master_leaves_a_stopped_swarm(store, monkeypatch, state):
    from scripts.swarm import tick
    from tests.swarm.test_tick import FakeRuntime

    monkeypatch.setenv("SWARM_HIVE_ID", "home")
    store.update("sw", state=state)
    agent(store, "master@a1b2c3-0001", FRONT_DOOR, lane=MASTER)

    assert tick.respawn_master("sw", store, FakeRuntime(), START) == []
    assert "master@a1b2c3-0001" in {a.name for a in store.agents("sw")}


def test_respawn_master_leaves_a_stopping_swarm(store, monkeypatch):
    from scripts.swarm import tick
    from tests.swarm.test_tick import FakeRuntime

    monkeypatch.setenv("SWARM_HIVE_ID", "home")
    store.update("sw", state="stopping")
    agent(store, "master@a1b2c3-0001", FRONT_DOOR, lane=MASTER)
    runtime = FakeRuntime()

    assert tick.respawn_master("sw", store, runtime, START) == []
    assert runtime.masters == []
    assert "master@a1b2c3-0001" in {a.name for a in store.agents("sw")}


@pytest.mark.parametrize("state", ["finished", "starting"])
def test_respawn_master_keeps_a_finished_or_starting_master_record(store, monkeypatch, state):
    from scripts.swarm import tick
    from tests.swarm.test_tick import FakeRuntime

    monkeypatch.setenv("SWARM_HIVE_ID", "home")
    agent(store, "master@a1b2c3-0001", FRONT_DOOR, lane=MASTER, state=state)

    actions = tick.respawn_master("sw", store, FakeRuntime(), START)

    assert "dropped the gone master master@a1b2c3-0001" not in actions
    assert "master@a1b2c3-0001" in {a.name for a in store.agents("sw")}
