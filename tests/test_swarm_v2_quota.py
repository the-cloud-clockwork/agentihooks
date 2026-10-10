import json
from dataclasses import replace
from pathlib import Path

import pytest

from hooks.context import quota_policy as qp
from scripts.swarm.keyspace import ROOT
from scripts.swarm.store import SwarmError
from scripts.swarm_v2 import quota
from tests.sv2_ctl02_cases import build

pytestmark = pytest.mark.unit

INPUTS = json.loads((Path(__file__).parent / "fixtures/swarm_v2/quota-observations.json").read_text())
SLUG = "fixture"
ACCOUNT, HARNESS, SPARE = INPUTS["account"], INPUTS["harness"], INPUTS["handoff_account"]
EXHAUSTED, STALE_FULL = INPUTS["exhausted"], INPUTS["stale_full"]
NOW = INPUTS["clock_ms"]
CANARY = "-".join(("leak", "canary", "fixture"))


class World:
    def __init__(self, monkeypatch):
        self.store, self.authority, _, self.clock, self.start = build(monkeypatch)
        self.accounts = {}
        self.quota = self.observer()
        self.seats = 0

    def authorize(self, token):
        grant = self.authority.authorize(token)
        return replace(grant, account=self.accounts.get(token, grant.account))

    def observer(self, store=None, slug=SLUG):
        return quota.QuotaObservations(store or self.store, slug, self.authorize, lambda: self.clock[0])

    def token(self, account=ACCOUNT):
        self.seats += 1
        _, token = self.start(seat=f"eng-{self.seats}@fixture")
        self.accounts[token] = account
        return token

    def publish(self, report, account=ACCOUNT, harness=HARNESS):
        return self.quota.publish(self.token(account), harness, dict(report))

    def stored(self):
        redis, found = self.store.redis, {}
        for key in sorted(redis.scan_iter(match=f"{ROOT}:quota*")):
            kind = redis.type(key)
            found[key] = (
                redis.hgetall(key) if kind == "hash" else redis.lrange(key, 0, -1) if kind == "list" else redis.get(key)
            )
        return found


@pytest.fixture
def world(monkeypatch):
    found = World(monkeypatch)
    found.clock[0] = NOW
    return found


def refusal(call, *args):
    with pytest.raises(SwarmError) as error:
        call(*args)
    return str(error.value)


@pytest.mark.parametrize("run", ["first", "second"])
def test_a_stale_full_report_after_a_newer_exhausted_one_keeps_the_exhausted_reading(world, run):
    newest = world.publish(EXHAUSTED)
    kept = world.publish(STALE_FULL)
    assert kept == newest == world.quota.latest(ACCOUNT, HARNESS)
    assert (newest.account, newest.harness, newest.five_used, newest.observed_ms) == (ACCOUNT, HARNESS, 100.0, 600000)
    reading = world.quota.reading(ACCOUNT, HARNESS)
    assert (reading.state, reading.routing_left, reading.age_seconds) == (quota.OBSERVED, 0.0, 60.0)
    assert world.quota.cap(ACCOUNT, HARNESS) == 0
    assert world.quota.quota_observation_age_seconds(ACCOUNT, HARNESS) == 60.0
    assert world.quota.stale_reports(ACCOUNT, HARNESS) == 1
    assert [entry.observed_ms for entry in world.quota.history(ACCOUNT, HARNESS)] == [600000]


def test_the_source_execution_and_account_come_from_the_grant(world):
    token = world.token()
    grant = world.authorize(token)
    observation = world.quota.publish(token, HARNESS, dict(EXHAUSTED))
    assert (observation.account, observation.execution_id, observation.generation) == (
        grant.account,
        grant.execution_id,
        grant.generation,
    )


def test_a_newer_report_replaces_an_older_one(world):
    world.publish(STALE_FULL)
    newest = world.publish(EXHAUSTED)
    assert world.quota.latest(ACCOUNT, HARNESS) == newest
    assert [entry.observed_ms for entry in world.quota.history(ACCOUNT, HARNESS)] == [600000, 300000]
    assert world.quota.stale_reports(ACCOUNT, HARNESS) == 0


def test_routing_uses_the_newest_fleet_observation_over_a_stale_local_cache(world, monkeypatch):
    from scripts import claude_quota_balancer as balancer

    world.publish(EXHAUSTED)
    world.publish(STALE_FULL)
    world.publish(INPUTS["spare"], account=SPARE)
    stale_local = replace(world.quota.latest(ACCOUNT, HARNESS), five_used=0.0, observed_ms=300000).probe()
    monkeypatch.setattr(balancer, "cached_observations", lambda: [(300.0, stale_local)])
    fleet = quota.latest_all(world.store.redis, HARNESS)
    others = qp._other_accounts({}, [(found.observed_ms / 1000, found.probe()) for found in fleet.values()])
    assert {c.account: c.five_used for c in others} == {ACCOUNT: 100.0, SPARE: 20.0}
    decision = qp.decide(
        account="spent",
        five_used=100.0,
        week_used=50.0,
        five_reset=18000,
        week_reset=600000,
        others=others,
        push=False,
    )
    assert (decision.action, decision.target.account) == ("handoff", SPARE)


def test_many_machines_reporting_one_account_stay_one_reading(world):
    for offset in (0, 10, 20):
        world.publish({**INPUTS["spare"], "observed_ms": INPUTS["spare"]["observed_ms"] + offset}, account=SPARE)
    assert list(quota.latest_all(world.store.redis, HARNESS)) == [SPARE]
    assert world.quota.latest(SPARE, HARNESS).observed_ms == INPUTS["spare"]["observed_ms"] + 20
    assert world.quota.cap(SPARE, HARNESS) == 6


def test_harnesses_keep_separate_readings(world):
    world.publish(EXHAUSTED)
    assert world.quota.reading(ACCOUNT, "codex").state == quota.UNKNOWN
    assert quota.latest_all(world.store.redis, "codex") == {}


@pytest.mark.parametrize(
    "report",
    [
        None,
        {"provider_status": "error", "observed_ms": 600000},
        {**STALE_FULL, "five_used": None},
        {**STALE_FULL, "week_used": None},
        {**STALE_FULL, "provider_status": "error"},
    ],
)
def test_missing_or_failed_quota_data_is_unknown_never_full(world, report):
    if report:
        world.publish(report)
    reading = world.quota.reading(ACCOUNT, HARNESS)
    assert (reading.state, reading.routing_left) == (quota.UNKNOWN, None)
    assert world.quota.cap(ACCOUNT, HARNESS) == 0
    assert world.quota.admit(ACCOUNT, HARNESS).action == quota.WAIT


def test_an_aged_reading_is_unknown(world):
    world.publish(STALE_FULL)
    world.clock[0] = STALE_FULL["observed_ms"] + quota.FRESH_SECONDS * 1000 + 1
    assert world.quota.reading(ACCOUNT, HARNESS).state == quota.UNKNOWN
    assert world.quota.quota_observation_age_seconds(ACCOUNT, HARNESS) == pytest.approx(quota.FRESH_SECONDS + 0.001)
    world.clock[0] -= 1
    assert world.quota.reading(ACCOUNT, HARNESS).state == quota.OBSERVED


def test_a_rejected_provider_status_leaves_no_room(world):
    world.publish({**STALE_FULL, "provider_status": "rejected"})
    reading = world.quota.reading(ACCOUNT, HARNESS)
    assert (reading.state, reading.routing_left) == (quota.OBSERVED, 0.0)
    assert world.quota.admit(ACCOUNT, HARNESS).reason == "exhausted"


def test_a_passed_reset_restores_its_window(world):
    world.publish({**EXHAUSTED, "five_reset": 600})
    assert world.quota.reading(ACCOUNT, HARNESS).routing_left == 38.0
    world.publish({**EXHAUSTED, "observed_ms": 610000, "five_reset": 660, "week_reset": 660})
    assert world.quota.reading(ACCOUNT, HARNESS).routing_left == 100.0


@pytest.mark.parametrize(
    ("harness", "report"),
    [
        ("claude", {**STALE_FULL, "token": CANARY}),
        ("claude", {**STALE_FULL, "infrastructure_budget": 40}),
        ("claude", {**STALE_FULL, "operator_budget": 40}),
        ("claude", {**STALE_FULL, "account": SPARE}),
        ("claude", {**STALE_FULL, "provider_status": CANARY}),
        ("claude", {**STALE_FULL, "five_used": CANARY}),
        ("claude", {**STALE_FULL, "five_used": True}),
        ("claude", {**STALE_FULL, "five_used": 100.5}),
        ("claude", {**STALE_FULL, "week_used": -1}),
        ("claude", {**STALE_FULL, "five_reset": CANARY}),
        ("claude", {**STALE_FULL, "week_reset": 0}),
        ("claude", {**STALE_FULL, "week_reset": True}),
        ("claude", {**STALE_FULL, "observed_ms": True}),
        ("claude", {**STALE_FULL, "observed_ms": 1.5}),
        ("claude", {**STALE_FULL, "observed_ms": 0}),
        ("claude", {**STALE_FULL, "observed_ms": NOW + quota.SKEW_MS + 1}),
        ("claude", {key: value for key, value in STALE_FULL.items() if key != "observed_ms"}),
        ("claude", {key: value for key, value in STALE_FULL.items() if key != "provider_status"}),
        ("claude", [CANARY]),
        (CANARY, STALE_FULL),
    ],
)
def test_reports_outside_the_provider_schema_are_refused_without_writing_or_echoing(world, harness, report):
    world.publish(EXHAUSTED)
    token = world.token()
    before = world.stored()
    error = refusal(world.quota.publish, token, harness, report)
    assert error == "invalid_request"
    assert world.stored() == before


@pytest.mark.parametrize("report", [{**STALE_FULL, "observed_ms": NOW + quota.SKEW_MS}, {**STALE_FULL, "five_used": 0}])
def test_reports_at_the_schema_limits_are_accepted(world, report):
    observation = world.publish(report)
    assert world.quota.latest(ACCOUNT, HARNESS) == observation


def test_nothing_but_the_provider_windows_reaches_the_store(world):
    token = world.token()
    world.quota.publish(token, HARNESS, dict(EXHAUSTED))
    stored = json.loads(world.store.redis.hget(f"{ROOT}:quota:{HARNESS}", ACCOUNT))
    assert sorted(stored) == sorted(["account", "harness", "execution_id", "generation", *quota.REPORT_FIELDS])
    assert token not in json.dumps(world.stored())


def test_a_report_without_a_grant_of_this_swarm_is_refused(world):
    token = world.token()
    before = world.stored()
    assert refusal(world.quota.publish, "", HARNESS, dict(STALE_FULL)) == "forbidden_scope"
    refused = quota.QuotaObservations(world.store, SLUG, lambda _: None, lambda: world.clock[0])
    assert refusal(refused.publish, token, HARNESS, dict(STALE_FULL)) == "forbidden_scope"
    assert refusal(world.observer(slug="other").publish, token, HARNESS, dict(STALE_FULL)) == "forbidden_scope"
    assert world.stored() == before


def test_a_known_account_with_room_is_admitted(world):
    world.publish(INPUTS["spare"], account=SPARE)
    assert world.quota.admit(SPARE, HARNESS) == quota.Admission(quota.ADMIT, SPARE)
    assert world.quota.admit(SPARE, HARNESS, SPARE) == quota.Admission(quota.ADMIT, SPARE)


def test_an_exhausted_account_hands_off_to_its_configured_account(world):
    world.publish(EXHAUSTED)
    world.publish(INPUTS["spare"], account=SPARE)
    assert world.quota.admit(ACCOUNT, HARNESS, SPARE) == quota.Admission(quota.HANDOFF, SPARE, 0, "exhausted")


def test_a_handoff_target_needs_the_minimum_room(world):
    world.publish({**INPUTS["spare"], "five_used": 100 - quota.MIN_ROUTING_LEFT}, account=SPARE)
    assert world.quota.admit(SPARE, HARNESS).action == quota.ADMIT
    world.publish({**INPUTS["spare"], "observed_ms": 500001, "five_used": 95.5}, account=SPARE)
    assert world.quota.admit(ACCOUNT, HARNESS, SPARE).action == quota.WAIT


def test_a_provider_outage_waits_a_bounded_time_without_new_starts(world):
    world.publish(EXHAUSTED)
    world.publish({**EXHAUSTED, "provider_status": "error", "observed_ms": 650000, "five_used": None})
    first = world.quota.admit(ACCOUNT, HARNESS, SPARE)
    assert first == quota.Admission(quota.WAIT, ACCOUNT, NOW + quota.WAIT_MS, "unknown")
    world.clock[0] += 1000
    assert world.quota.admit(ACCOUNT, HARNESS, SPARE) == first
    world.clock[0] = first.until_ms - 1
    assert world.quota.admit(ACCOUNT, HARNESS) == first
    world.clock[0] = first.until_ms
    assert world.quota.admit(ACCOUNT, HARNESS).until_ms == first.until_ms + quota.WAIT_MS
    assert [entry.observed_ms for entry in world.quota.history(ACCOUNT, HARNESS)] == [650000, 600000]


def test_waits_are_kept_per_account_and_harness(world):
    first = world.quota.admit(ACCOUNT, HARNESS)
    world.clock[0] += 1000
    assert world.quota.admit(SPARE, HARNESS).until_ms == first.until_ms + 1000
    assert world.quota.admit(ACCOUNT, "codex").until_ms == first.until_ms + 1000
    assert world.quota.admit(ACCOUNT, HARNESS) == first


def test_recovery_replays_one_effect_and_keeps_the_newer_result(world):
    token = world.token()
    first = world.quota.publish(token, HARNESS, dict(EXHAUSTED))
    assert world.quota.publish(token, HARNESS, dict(EXHAUSTED)) == first
    assert world.quota.stale_reports(ACCOUNT, HARNESS) == 0
    restarted = world.observer()
    assert restarted.publish(world.token(), HARNESS, dict(STALE_FULL)) == first
    assert restarted.stale_reports(ACCOUNT, HARNESS) == 1
    assert len(restarted.history(ACCOUNT, HARNESS)) == 1


def test_history_is_bounded(world):
    token = world.token()
    for step in range(quota.HISTORY + 3):
        world.quota.publish(token, HARNESS, {**STALE_FULL, "observed_ms": 1000 + step})
    found = world.quota.history(ACCOUNT, HARNESS)
    assert len(found) == quota.HISTORY
    assert (found[0].observed_ms, found[-1].observed_ms) == (1000 + quota.HISTORY + 2, 1003)


class Racing:
    """A store whose pipelines run `before` ahead of each commit."""

    def __init__(self, store, before):
        self.store, self.before = store, before

    @property
    def redis(self):
        return self

    def pipeline(self):
        pipe = self.store.redis.pipeline()
        real = pipe.execute

        def execute():
            self.before()
            return real()

        pipe.execute = execute
        return pipe

    def __getattr__(self, name):
        return getattr(self.store.redis, name)


def test_a_concurrent_newer_publish_wins_the_retry(world):
    from redis.exceptions import WatchError

    token, rival = world.token(), world.token()
    raced = []

    def race():
        if not raced:
            raced.append(1)
            world.quota.publish(rival, HARNESS, dict(EXHAUSTED))
            raise WatchError

    kept = world.observer(Racing(world.store, race)).publish(token, HARNESS, dict(STALE_FULL))
    assert kept.observed_ms == EXHAUSTED["observed_ms"]
    assert world.quota.stale_reports(ACCOUNT, HARNESS) == 1


def test_writes_give_up_after_their_attempts(world):
    from redis.exceptions import WatchError

    token = world.token()
    attempts = []

    def fail():
        attempts.append(1)
        raise WatchError

    observer = world.observer(Racing(world.store, fail))
    assert refusal(observer.publish, token, HARNESS, dict(STALE_FULL)) == "dependency_unavailable"
    assert refusal(observer.admit, ACCOUNT, HARNESS) == "dependency_unavailable"
    assert len(attempts) == 2 * quota.WRITE_ATTEMPTS
    assert world.stored() == {}


def test_an_observation_round_trips_and_converts_to_a_probe_result(world):
    observation = world.publish(EXHAUSTED)
    assert quota.decode(quota.encode(observation)) == observation
    probe = observation.probe()
    assert (probe.account, probe.provider_status, probe.five_hour, probe.seven_day) == (
        ACCOUNT,
        "allowed_warning",
        quota.QuotaWindow(100.0, 18000),
        quota.QuotaWindow(62.0, 600000),
    )
    failed = replace(observation, provider_status="error").probe()
    assert (failed.five_hour, failed.seven_day) == (quota.QuotaWindow(), quota.QuotaWindow())


def test_fleet_observations_are_off_unless_enabled(world, monkeypatch):
    world.publish(EXHAUSTED)
    environs = []

    def client(environ):
        environs.append(environ)
        return world.store.redis

    monkeypatch.setattr("scripts.swarm.store.redis_client", client)
    assert quota.fleet_observations({}) == []
    assert quota.fleet_observations({quota.FLAG: "yes"}) == []
    environ = {quota.FLAG: "1"}
    [(observed_at, probe)] = quota.fleet_observations(environ)
    assert (observed_at, probe.account, probe.five_hour.used) == (600.0, ACCOUNT, 100.0)
    assert quota.fleet_observations(environ, "codex") == []
    assert environs == [environ, environ]


def test_an_unreachable_fleet_store_falls_back_to_the_local_cache(monkeypatch):
    from redis.exceptions import ConnectionError

    def unreachable(environ):
        raise ConnectionError("down")

    monkeypatch.setattr("scripts.swarm.store.redis_client", unreachable)
    assert quota.fleet_observations({quota.FLAG: "1"}) == []


def test_the_default_clock_is_the_redis_server_clock(world):
    observer = quota.QuotaObservations(world.store, SLUG, world.authorize)
    assert abs(observer.clock() - world.store.redis.time()[0] * 1000) < 2000
