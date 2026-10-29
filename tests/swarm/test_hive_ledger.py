import time

import pytest

from scripts.hive import registry, scope
from scripts.swarm import hive_ledger, outage
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.unit

NAME, OTHER = "engineer@a1b2c3-0001", "engineer@a1b2c3-0002"
HIVE = {
    "AGENTIHOOKS_SWARM": "sw",
    "AGENTIHOOKS_AGENT_NAME": NAME,
    "AGENTIHOOKS_HIVE_ID": "h1",
    "AGENTIHOOKS_DEPLOYMENT": "hive",
    "AGENTIHOOKS_HIVE_REDIS_URL": "redis://hive.test:6379",
}


@pytest.fixture(autouse=True)
def outages(tmp_path, monkeypatch):
    monkeypatch.setattr(outage, "HOME", tmp_path)


@pytest.fixture
def store():
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    for name, task in ((NAME, "t1"), (OTHER, "t2")):
        s.put_agent("sw", AgentRecord(name, "eng", task))
    return s


@pytest.fixture
def hive(monkeypatch):
    for name, value in HIVE.items():
        monkeypatch.setenv(name, value)


def failed(store, name, at, code=502):
    hive_ledger.record(store.redis, "sw", name, code, at)


def three_failures(store):
    for at in (1_000, 2_000, 3_000):
        failed(store, NAME, at)
    hive_ledger.observe(store, "sw", 3_500)


def test_repeated_hive_gateway_failures_open_the_outage_with_their_evidence(store):
    failed(store, NAME, 1_000)
    failed(store, OTHER, 2_000, 504)
    hive_ledger.observe(store, "sw", 2_500)
    assert outage.open_records("sw") == []
    failed(store, NAME, 3_000)
    hive_ledger.observe(store, "sw", 3_500)
    [record] = outage.open_records("sw")
    evidence = (
        f"Hive agent ledger writes failed 3 times with gateway errors since 1000, from {NAME}, {OTHER}, last HTTP 502"
    )
    assert record == {"kind": "hive-ledger", "started_at": 3_500, "evidence": [evidence], "ended_at": None}
    assert outage.held(store, "sw", NAME) == evidence


def test_the_first_successful_hive_write_ends_the_outage_and_earlier_failures_stop_counting(store):
    three_failures(store)
    hive_ledger.record(store.redis, "sw", OTHER, None, 4_000)
    hive_ledger.observe(store, "sw", 4_500)
    assert outage.open_records("sw") == []
    assert outage.read("sw")["hive-ledger"]["ended_at"] == 4_500
    failed(store, NAME, 5_000)
    hive_ledger.observe(store, "sw", 5_500)
    assert outage.open_records("sw") == []


def test_a_failure_after_an_agent_success_keeps_that_success_for_the_swarm(store):
    three_failures(store)
    hive_ledger.record(store.redis, "sw", OTHER, None, 4_000)
    failed(store, OTHER, 4_600)
    assert hive_ledger.read(store.redis, "sw", OTHER) == {"ok_at": 4_000, "failed": [[4_600, 502]]}
    hive_ledger.observe(store, "sw", 4_700)
    assert outage.open_records("sw") == []


def test_a_workstation_success_leaves_the_hive_outage_open(store, monkeypatch):
    three_failures(store)
    for name in HIVE:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "sw")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", OTHER)
    assert hive_ledger.report(None, redis=store.redis, now_ms=4_000) is False
    assert outage.ledger_health("sw", 4_000, lambda: True)
    hive_ledger.observe(store, "sw", 4_500)
    assert [record["kind"] for record in outage.open_records("sw")] == ["hive-ledger"]
    assert outage.read("sw")["hive-ledger"]["started_at"] == 3_500


def test_old_failures_open_nothing_but_an_open_outage_waits_for_a_success(store):
    for at in (1_000, 2_000, 3_000):
        failed(store, NAME, at)
    hive_ledger.observe(store, "sw", 1_000 + hive_ledger.WINDOW_MS + 1)
    assert outage.open_records("sw") == []
    hive_ledger.observe(store, "sw", 1_000 + hive_ledger.WINDOW_MS)
    hive_ledger.observe(store, "sw", 3_000 + 10 * hive_ledger.WINDOW_MS)
    assert [record["started_at"] for record in outage.open_records("sw")] == [1_000 + hive_ledger.WINDOW_MS]


def test_a_hive_agent_reports_each_write_result_in_its_own_readings(store, hive):
    key = f"{registry.key('h1')}:swarm:sw:ledger-writes:{NAME}"
    assert hive_ledger.report(502, redis=store.redis, now_ms=1_000) is True
    assert hive_ledger.report(503, redis=store.redis, now_ms=2_000) is True
    assert hive_ledger.read(store.redis, "sw", NAME) == {"failed": [[1_000, 502], [2_000, 503]]}
    assert 0 < store.redis.ttl(key) <= 24 * 3600
    assert hive_ledger.report(None, redis=store.redis, now_ms=3_000) is True
    assert hive_ledger.read(store.redis, "sw", NAME) == {"ok_at": 3_000}


def test_a_hive_agent_keeps_only_its_latest_failures(store):
    for at in range(hive_ledger.KEPT + 2):
        failed(store, NAME, at)
    assert hive_ledger.read(store.redis, "sw", NAME)["failed"][0] == [2, 502]
    assert len(hive_ledger.read(store.redis, "sw", NAME)["failed"]) == hive_ledger.KEPT


def test_a_report_without_a_redis_connects_and_stamps_the_current_time(store, hive, monkeypatch):
    from scripts.swarm import store as store_module

    monkeypatch.setattr(store_module, "redis_client", lambda environ: store.redis)
    before = time.time_ns() // 1_000_000
    assert hive_ledger.report(502) is True
    [[at, code]] = hive_ledger.read(store.redis, "sw", NAME)["failed"]
    assert before <= at <= time.time_ns() // 1_000_000 and code == 502


@pytest.mark.parametrize("missing", ["AGENTIHOOKS_SWARM", "AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_HIVE_ID"])
def test_a_report_outside_a_hive_swarm_agent_records_nothing(store, hive, monkeypatch, missing):
    monkeypatch.delenv(missing)
    assert hive_ledger.report(502, redis=store.redis, now_ms=1_000) is False
    assert hive_ledger.read(store.redis, "sw", NAME) == {}


def test_failures_from_the_first_instant_count_and_a_success_at_the_same_instant_hides_them(store):
    for at in (0, 1, 2):
        failed(store, NAME, at)
    hive_ledger.observe(store, "sw", 3)
    assert [record["kind"] for record in outage.open_records("sw")] == ["hive-ledger"]
    hive_ledger.record(store.redis, "sw", OTHER, None, 2)
    hive_ledger.observe(store, "sw", 4)
    assert outage.open_records("sw") == []
    for at in (5, 6):
        failed(store, NAME, at)
    hive_ledger.observe(store, "sw", 7)
    assert outage.open_records("sw") == []


def test_the_tick_reads_a_hive_agent_report_below_the_member_that_runs_it(store, hive, monkeypatch):
    assert hive_ledger.report(502, redis=store.redis, now_ms=1_000) is True
    hive_ledger.report(503, redis=store.redis, now_ms=2_000)
    hive_ledger.report(504, redis=store.redis, now_ms=3_000)
    for name in HIVE:
        monkeypatch.delenv(name)
    placed = {("sw", NAME): "h1"}
    monkeypatch.setattr(scope, "host", lambda redis, slug, name: placed.get((slug, name), ""))
    assert hive_ledger.read(store.redis, "sw", NAME) == {"failed": [[1_000, 502], [2_000, 503], [3_000, 504]]}
    hive_ledger.observe(store, "sw", 3_500)
    [record] = outage.open_records("sw")
    assert record["evidence"] == [
        f"Hive agent ledger writes failed 3 times with gateway errors since 1000, from {NAME}, last HTTP 504"
    ]


def test_a_report_reads_the_environment_it_is_given(store, monkeypatch):
    from scripts.swarm import store as store_module

    for name in HIVE:
        monkeypatch.delenv(name, raising=False)
    environ = dict(HIVE)
    monkeypatch.setattr(store_module, "redis_client", lambda env: store.redis if env is environ else None)
    assert hive_ledger.report(502, environ=environ, now_ms=1_000) is True
    assert hive_ledger.read(store.redis, "sw", NAME) == {"failed": [[1_000, 502]]}


def test_a_report_on_an_unreachable_redis_is_dropped(hive):
    from redis.exceptions import RedisError

    class Down:
        def __getattr__(self, name):
            def fail(*args, **kwargs):
                raise RedisError("down")

            return fail

    assert hive_ledger.report(502, redis=Down(), now_ms=1_000) is False
