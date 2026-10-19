import json
import os

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import cli, controller_probes, lease, tick_heartbeat, tick_watch
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]
MINUTE = 60_000
LIMIT = tick_heartbeat.LIMIT_MS
START = 1_800_000_000_000
FRONT_DOOR = "master@sw"
PROBE_ENV = {"AGENTIHOOKS_CONTROL_API_PORT": "8780", "AGENTIHOOKS_CONTROL_SWARM": "sw"}


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


def test_a_seat_that_comes_live_during_the_episode_gets_the_same_alert(store):
    beat(store, START)
    tick_watch.watch_pass(store, "sw", START + LIMIT, START)
    agent(store, "engineer@a1b2c3-0001", "eng-1@sw")

    assert tick_watch.watch_pass(store, "sw", START + LIMIT + MINUTE, START) == ["told eng-1@sw the tick is stale"]
    assert texts(store, "eng-1@sw") == texts(store, FRONT_DOOR)


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


def test_the_liveness_probe_leaves_a_swarm_whose_ledger_is_gone(store, lease_clock):
    beat(store, START - 3 * LIMIT)
    store.redis.set(store.key("sw", "ledger-gone"), 1)

    assert probe(store, age=2 * LIMIT) == 0


def test_the_watcher_respawns_the_master_only_while_the_tick_is_stale(store):
    asked = []
    beat(store, START)

    tick_watch.watch_pass(store, "sw", START + MINUTE, START, lambda slug: asked.append(slug) or [])
    assert asked == []
    assert tick_watch.watch_pass(store, "sw", START + LIMIT, START, lambda slug: [f"spawned master for {slug}"])[
        -1
    ] == ("spawned master for sw")


def test_the_watcher_run_passes_over_every_swarm_once_a_minute(store, capsys):
    store.create(SwarmConfig("other", "/repo", max_eng=1, max_ci=0))
    clock = [START]
    waits = []

    def wait(seconds):
        waits.append(seconds)
        clock[0] += LIMIT

    tick_watch.run(store, lambda: clock[0], None, wait, passes=2)

    assert waits == [tick_watch.PASS_S, tick_watch.PASS_S]
    assert "sw: told master@sw the tick is stale" in capsys.readouterr().out


def test_respawn_master_spawns_a_master_into_an_empty_front_door_seat(store, monkeypatch):
    from scripts.swarm import tick
    from tests.swarm.test_tick import FakeRuntime

    monkeypatch.setenv("SWARM_HIVE_ID", "home")
    runtime = FakeRuntime()
    agent(store, "master@a1b2c3-0001", FRONT_DOOR, lane=MASTER)

    actions = tick.respawn_master("sw", store, runtime, START)

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


def probe(store, age, owner="home"):
    return controller_probes.live(PROBE_ENV, store, lambda url, timeout: None, owner, lambda: age)


@pytest.fixture
def lease_clock(store, monkeypatch):
    now = [START]
    monkeypatch.setattr(lease, "now_ms", lambda saved: now[0])
    assert lease.acquire(store, "sw", "home") is not None
    return now


def test_the_liveness_probe_fails_when_the_tick_it_runs_has_not_completed_a_pass_for_ten_minutes(
    store, lease_clock, capsys
):
    beat(store, START - LIMIT)

    assert probe(store, age=2 * LIMIT) == 1
    assert capsys.readouterr().out == (
        "controller: the tick is stale, sw: no tick pass has completed for 10 minutes, the last ended at "
        f"{tick_heartbeat.clock(START - LIMIT)}\n"
    )


def test_the_liveness_probe_fails_on_a_frozen_decision_input(store, lease_clock, capsys):
    beat(store, START, host=START - LIMIT)

    assert probe(store, age=2 * LIMIT) == 1
    assert "the host room it decides on was 10 minutes old at its last pass" in capsys.readouterr().out


def test_the_liveness_probe_passes_a_fresh_heartbeat(store, lease_clock, capsys):
    beat(store, START - MINUTE)

    assert probe(store, age=2 * LIMIT) == 0
    assert capsys.readouterr().out == "controller: live\n"


def test_a_restarted_tick_gets_ten_minutes_from_its_own_start(store, lease_clock):
    beat(store, START - 3 * LIMIT)

    assert probe(store, age=LIMIT - 1) == 0
    assert probe(store, age=LIMIT) == 1


def test_a_restarted_tick_with_no_heartbeat_fails_after_ten_minutes(store, lease_clock):
    assert probe(store, age=LIMIT - 1) == 0
    assert probe(store, age=LIMIT) == 1


def test_the_liveness_probe_judges_only_the_swarms_this_controller_ticks(store, lease_clock):
    beat(store, START - 3 * LIMIT)

    assert probe(store, age=2 * LIMIT, owner="anton") == 0


def test_the_liveness_probe_skips_a_stopped_swarm(store, lease_clock):
    beat(store, START - 3 * LIMIT)
    store.update("sw", state="stopped")

    assert probe(store, age=2 * LIMIT) == 0


def test_the_liveness_probe_leaves_a_swarm_in_a_ledger_outage_to_the_watcher(store, lease_clock, tmp_path, monkeypatch):
    from scripts.swarm import outage

    monkeypatch.setattr(outage, "HOME", tmp_path)
    beat(store, START - 3 * LIMIT)
    outage.ledger_health("sw", START - 2 * LIMIT, lambda: False)

    assert probe(store, age=2 * LIMIT) == 0
    assert tick_watch.watch_pass(store, "sw", START, START) == [f"told {FRONT_DOOR} the tick is stale"]


def test_the_liveness_probe_without_a_control_api_judges_the_lease_and_heartbeat_only(store, lease_clock, capsys):
    beat(store, START - MINUTE)

    assert controller_probes.live({}, store, None, "home", lambda: 2 * LIMIT) == 0
    assert capsys.readouterr().out == "controller: live\n"


def test_a_restarted_watcher_alerts_at_once_on_a_heartbeat_already_ten_minutes_old(store):
    beat(store, START)

    assert tick_watch.watch_pass(store, "sw", START + LIMIT, START + LIMIT) == [f"told {FRONT_DOOR} the tick is stale"]


def test_the_process_age_reads_the_kernel_start_time():
    assert 0 <= tick_heartbeat.process_age_ms(os.getpid()) < 3_600_000
