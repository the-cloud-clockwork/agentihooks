import importlib
import io
import json
import sys
import urllib.error
from types import SimpleNamespace

import pytest

from scripts import swarm
from scripts.gates import Who
from scripts.swarm import lease, ledger_client, ledger_cutover
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm_ledger import ledger_route
from scripts.swarm_ledger.api import client as api_client
from scripts.swarm_ledger.api import controller_lease
from scripts.swarm_ledger.api.errors import APIError
from scripts.swarm_v2 import masters

SLUG = "sw"
DEST = "https://hosted.example"
TOKEN = {"AGENTIHOOKS_LEDGER_AGENT_TOKEN": "token-1"}
NOW = 10000


@pytest.fixture
def store(monkeypatch):
    import fakeredis

    result = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    result.create(SwarmConfig(SLUG, "/repo", 1, 0, state="paused"))
    monkeypatch.setattr(lease, "now_ms", lambda *_: NOW)
    result.update(SLUG, pending_api_url=DEST)
    return result


def start(store, lane, seat, launched_at=NOW + 1):
    name = store.next_name(SLUG, lane)
    return store.start_execution(SLUG, AgentRecord(name, lane, "proof", seat=seat, launched_at=launched_at))


@pytest.fixture
def worker(store):
    return start(store, "eng", f"eng-1@{SLUG}")


class Probe:
    def __init__(self):
        self.calls = []
        self.error = None
        self.hook = None

    def __call__(self, url, headers):
        probe = self

        class Client:
            def request(self, slug, path, payload=None):
                probe.calls.append((url, headers, slug, path))
                if probe.hook:
                    probe.hook()
                if probe.error:
                    raise probe.error
                return {"data": {}}

        return Client()


@pytest.fixture
def probe(monkeypatch):
    found = Probe()
    monkeypatch.setattr(api_client, "ResourceClient", found)
    return found


class Spy:
    def __init__(self, pipe, watched, fail, fail_watch=False):
        self.pipe, self.watched, self.fail, self.fail_watch = pipe, watched, fail, fail_watch

    def __enter__(self):
        self.pipe.__enter__()
        return self

    def __exit__(self, *exc):
        return self.pipe.__exit__(*exc)

    def watch(self, *keys):
        self.watched.append(keys)
        if self.fail_watch:
            from redis.exceptions import WatchError

            raise WatchError("connection lost while watching")
        return self.pipe.watch(*keys)

    def execute(self):
        if self.fail and self.pipe.watching:
            from redis.exceptions import WatchError

            raise WatchError("changed")
        return self.pipe.execute()

    def __getattr__(self, name):
        return getattr(self.pipe, name)


def spy(store, monkeypatch, fail=False, fail_watch=False):
    watched, real = [], store.redis.pipeline
    monkeypatch.setattr(store.redis, "pipeline", lambda *a, **k: Spy(real(*a, **k), watched, fail, fail_watch))
    return watched


def who(agent):
    return Who(name=agent.name, swarm=SLUG)


def receipts(store):
    return store.redis.hgetall(store.key(SLUG, ledger_cutover.READY))


def execution(store, agent, **changes):
    key = store.key(SLUG, "executions")
    row = json.loads(store.redis.hget(key, agent.execution_id))
    store.redis.hset(key, agent.execution_id, json.dumps({**row, **changes}))


def test_a_ready_successor_authenticates_once_and_records_its_receipt(store, worker, probe, monkeypatch):
    watched = spy(store, monkeypatch)
    assert ledger_cutover.acknowledge(store, SLUG, who(worker), TOKEN) is True
    assert probe.calls == [(DEST, {"X-Ledger-Token": "token-1", "X-Ledger-Agent": worker.name}, SLUG, "metadata")]
    assert watched[-1] == (store.key(SLUG, "config"), store.key(SLUG, "executions"))
    stamp = {"url": DEST, "staged_at": NOW, "execution_id": worker.execution_id, "generation": worker.generation}
    assert {name: json.loads(value) for name, value in receipts(store).items()} == {worker.name: stamp}
    probe.error = AssertionError("authenticated twice")
    assert ledger_cutover.acknowledge(store, SLUG, who(worker), TOKEN) is True


def test_a_client_patched_while_the_module_first_imports_is_not_kept(store, worker, monkeypatch):
    class Stale:
        def __init__(self, base, headers):
            raise AssertionError(f"kept the client patched at import for {base}")

    monkeypatch.delitem(sys.modules, ledger_cutover.__name__)
    monkeypatch.setattr(swarm, "ledger_cutover", ledger_cutover)
    with pytest.MonkeyPatch.context() as earlier:
        earlier.setattr(api_client, "ResourceClient", Stale)
        fresh = importlib.import_module(ledger_cutover.__name__)
    found = Probe()
    monkeypatch.setattr(api_client, "ResourceClient", found)
    assert fresh.acknowledge(store, SLUG, who(worker), TOKEN) is True
    assert [call[0] for call in found.calls] == [DEST]


def test_a_receipt_needs_a_pinned_token_a_staged_destination_and_a_fresh_execution(store, worker, probe):
    probe.error = AssertionError("authenticated without its preconditions")
    assert ledger_cutover.acknowledge(store, SLUG, Who(name=worker.name, swarm=""), TOKEN) is False
    assert ledger_cutover.acknowledge(store, SLUG, who(worker), {}) is False
    assert ledger_cutover.acknowledge(store, SLUG, Who(name="engineer@ffffff-0099", swarm=SLUG), TOKEN) is False
    early = start(store, "eng", f"eng-2@{SLUG}", launched_at=NOW - 1)
    assert ledger_cutover.acknowledge(store, SLUG, who(early), TOKEN) is False
    store.update(SLUG, pending_api_url="")
    assert ledger_cutover.acknowledge(store, SLUG, who(worker), TOKEN) is False
    assert receipts(store) == {}


def test_an_execution_launched_at_the_staging_time_counts_as_fresh(store, probe):
    agent = start(store, "eng", f"eng-2@{SLUG}", launched_at=NOW)
    assert ledger_cutover.acknowledge(store, SLUG, who(agent), TOKEN) is True
    assert list(receipts(store)) == [agent.name]


def test_an_unreachable_destination_records_no_receipt(store, worker, probe):
    probe.error = OSError("refused")
    assert ledger_cutover.acknowledge(store, SLUG, who(worker), TOKEN) is False
    assert receipts(store) == {}


@pytest.mark.parametrize(
    "change",
    [
        lambda store, agent: execution(store, agent, state="finished"),
        lambda store, agent: execution(store, agent, generation=agent.generation + 1),
        lambda store, agent: store.redis.hdel(store.key(SLUG, "executions"), agent.execution_id),
        lambda store, agent: store.redis.hset(store.key(SLUG, "config"), "ledger_staged_at", NOW + 5),
        lambda store, agent: store.redis.hset(store.key(SLUG, "config"), "pending_api_url", "https://other.example"),
    ],
)
def test_a_receipt_is_refused_when_the_execution_or_stage_moves_during_authentication(store, worker, probe, change):
    probe.hook = lambda: change(store, worker)
    assert ledger_cutover.acknowledge(store, SLUG, who(worker), TOKEN) is False
    assert receipts(store) == {}


def test_a_receipt_is_refused_when_its_transaction_is_interrupted(store, worker, probe, monkeypatch):
    spy(store, monkeypatch, fail=True)
    assert ledger_cutover.acknowledge(store, SLUG, who(worker), TOKEN) is False
    assert receipts(store) == {}


def test_a_stage_without_its_timestamp_still_accepts_a_receipt(store, worker, probe):
    store.redis.hdel(store.key(SLUG, "config"), "ledger_staged_at")
    assert ledger_cutover.acknowledge(store, SLUG, who(worker), TOKEN) is True


def test_readiness_reports_every_field_and_counts_each_master_seat(store, worker, probe):
    masters.MasterSeats(store.redis).set_count(SLUG, 2)
    lead = start(store, "master", f"{masters.seat(1)}@{SLUG}")
    gone = start(store, "eng", f"eng-2@{SLUG}")
    agents = store.key(SLUG, "agents")
    store.redis.hset(
        agents, gone.name, json.dumps({**json.loads(store.redis.hget(agents, gone.name)), "state": "finished"})
    )
    execution(store, gone, state="finished")
    assert ledger_cutover.readiness(store, SLUG) == {
        "ready": False,
        "missing": sorted([worker.name, lead.name]),
        "missing_masters": [f"{masters.seat(2)}@{SLUG}"],
        "pending_url": DEST,
        "active_url": "",
        "staged_at": NOW,
    }
    masters.MasterSeats(store.redis).set_count(SLUG, 1)
    assert ledger_cutover.readiness(store, SLUG)["missing_masters"] == []


def test_readiness_names_the_first_master_seat_while_no_master_runs(store, worker, probe):
    masters.MasterSeats(store.redis).set_count(SLUG, 1)
    assert ledger_cutover.readiness(store, SLUG)["missing_masters"] == [f"{masters.seat(1)}@{SLUG}"]


def test_settings_refuse_a_mixed_or_unstaged_endpoint_change(store):
    with pytest.raises(SwarmError) as mixed:
        ledger_cutover.settings(store, SLUG, {"api_url": DEST, "max_eng": 2})
    assert str(mixed.value) == "the ledger endpoint must be activated separately"
    with pytest.raises(SwarmError) as unstaged:
        ledger_cutover.settings(store, SLUG, {"api_url": "https://elsewhere.example"})
    assert str(unstaged.value) == "the ledger destination must be staged and ready"


def test_activation_watches_every_cohort_and_lease_key(store):
    assert ledger_cutover._activation_keys(store, SLUG) == {
        store.key(SLUG, "config"): "hgetall",
        store.key(SLUG, "agents"): "hgetall",
        store.key(SLUG, "executions"): "hgetall",
        store.key(SLUG, "desired-routes"): "hgetall",
        store.key(SLUG, "ledger-ready"): "hgetall",
        masters.MasterSeats(store.redis).key(SLUG): "hgetall",
        store.key(SLUG, "tick-lock"): "get",
        store.key(SLUG, "control-owner"): "get",
        store.key(SLUG, "control-epoch"): "get",
    }


@pytest.fixture
def controller(store, monkeypatch):
    from scripts.swarm_ledger.storage_migration import hosted

    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.setattr(hosted, "server_secret", lambda: "controller-secret")
    monkeypatch.setattr(ledger_cutover, "readiness", lambda *_: {"ready": True})
    return store


def refusal(store):
    with pytest.raises(SwarmError) as refused:
        ledger_cutover._activate(store, SLUG, DEST)
    assert store.config(SLUG).api_url == ""
    return str(refused.value)


def test_activation_names_each_refusal_before_it_moves_the_lease(controller, monkeypatch):
    from scripts.swarm import commands

    store = controller
    monkeypatch.setattr(ledger_cutover, "readiness", lambda *_: {"ready": False})
    assert refusal(store) == "the ledger successors are not ready"

    def drift(store, slug):
        store.redis.hset(store.key(slug, "desired-routes"), "engineer@ffffff-0001", "local")
        return {"ready": True}

    monkeypatch.setattr(ledger_cutover, "readiness", drift)
    assert refusal(store) == "the ledger activation cohort changed"
    monkeypatch.setattr(ledger_cutover, "readiness", lambda *_: {"ready": True})
    store.redis.hset(store.key(SLUG, "config"), "state", "running")
    assert refusal(store) == "the swarm must be paused before ledger activation"
    store.redis.hset(store.key(SLUG, "config"), "state", "paused")
    store.redis.set(store.key(SLUG, "tick-lock"), "tick")
    assert refusal(store) == "the swarm tick is still running"
    store.redis.delete(store.key(SLUG, "tick-lock"))
    other = {"owner": f"{commands.hive_id()}-other", "epoch": 3, "expires_at": 10**15}
    store.redis.set(store.key(SLUG, "control-owner"), json.dumps(other))
    assert refusal(store) == "the source controller ownership changed"
    store.redis.set(store.key(SLUG, "control-owner"), "legacy-owner")
    assert refusal(store) == "the source controller ownership changed"


def test_an_activation_interrupted_before_it_moves_the_lease_releases_nothing(controller, monkeypatch):
    def release(*_):
        raise AssertionError("released a lease that was never moved")

    monkeypatch.setattr(lease, "release", release)
    spy(controller, monkeypatch, fail_watch=True)
    assert refusal(controller) == "the ledger activation changed before commit"


def test_the_route_acknowledges_with_the_environment_it_checked(store, worker, monkeypatch):
    import os

    seen = []
    monkeypatch.delenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", raising=False)
    environment = {**os.environ, "AGENTIHOOKS_LEDGER_AGENT_TOKEN": "token-1"}
    monkeypatch.setattr(os, "environ", environment)
    monkeypatch.setattr(ledger_cutover, "acknowledge", lambda store, slug, who, environ=None: seen.append(environ))
    ledger_route._acknowledge(store, SLUG, who(worker))
    assert seen == [environment]


def test_a_pinned_caller_is_refused_activation(controller, worker, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", worker.name)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", SLUG)
    assert refusal(controller) == "ledger activation requires controller authority"


def test_controller_lease_actions_refuse_principals_and_stale_owners(store, monkeypatch):
    monkeypatch.setattr(controller_lease, "connect", lambda: store)
    with pytest.raises(APIError) as refused:
        controller_lease.act(SLUG, "engineer@a1b2c3-0001", {"action": "acquire", "owner": "hive"})
    assert (refused.value.status, refused.value.code, str(refused.value)) == (
        403,
        "controller_access_required",
        "Controller access required",
    )
    with pytest.raises(APIError) as stale:
        controller_lease.act(SLUG, "", {"action": "renew", "owner": "hive", "epoch": 3})
    assert (stale.value.status, stale.value.code, str(stale.value)) == (
        409,
        "stale_controller",
        "the controller lease is stale",
    )


@pytest.fixture
def hosted_client(monkeypatch):
    seen = []

    class Client:
        reply = {"data": None}
        error = None

        def __init__(self, base, headers):
            seen.append((base, headers))

        def request(self, slug, path, payload=None):
            if Client.error:
                raise Client.error
            return Client.reply

    monkeypatch.setattr(api_client, "ResourceClient", Client)
    credentials = SimpleNamespace(credentials=lambda slug, service: {"service": service})
    monkeypatch.setattr(ledger_client, "_ledger", lambda: credentials)
    return Client, seen


def test_hosted_lease_reads_use_the_caller_and_writes_use_the_service_credential(store, hosted_client):
    client, seen = hosted_client
    with ledger_route.scoped(SLUG, DEST):
        assert lease.current(store, SLUG) is None
        client.reply = {"data": {"owner": "hive", "epoch": 3, "expires_at": 5}}
        assert lease.acquire(store, SLUG, "hive") == lease.Lease("hive", 3, 5)
    assert seen == [(DEST, {"service": False}), (DEST, {"service": True})]


def refused(code, body):
    return urllib.error.HTTPError(DEST, code, "refused", {}, io.BytesIO(body))


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            refused(409, b'{"error": {"code": "stale_controller", "message": "the controller lease is stale"}}'),
            "hosted controller lease refused: 409 stale_controller the controller lease is stale",
        ),
        (refused(403, b"{}"), "hosted controller lease refused: 403  "),
        (OSError("ledger down"), "hosted controller lease is unavailable: ledger down"),
    ],
)
def test_hosted_lease_failures_name_the_refusal(store, hosted_client, error, message):
    client, _ = hosted_client
    client.error = error
    with ledger_route.scoped(SLUG, DEST), pytest.raises(SwarmError) as failed:
        lease.current(store, SLUG)
    assert str(failed.value) == message


def test_lease_calls_for_another_swarm_stay_local_while_one_is_routed(store, monkeypatch):
    def hosted(*_):
        raise AssertionError("another swarm's lease went to the hosted ledger")

    monkeypatch.setattr(lease, "_remote", hosted)
    with ledger_route.scoped("selected", DEST):
        held = lease.acquire(store, SLUG, "hive")
        assert lease.current(store, SLUG) == held
        assert lease.renew(store, SLUG, held).epoch == held.epoch
        assert lease.release(store, SLUG, held) is True
        assert lease.transfer(store, SLUG, "hive", 0, 0).epoch == held.epoch + 1
        assert lease._address(SLUG) == ""


def owner(store, raw):
    store.redis.set(store.key(SLUG, "control-owner"), raw)


def test_a_first_transfer_starts_at_epoch_one_with_an_expiring_lease(store, monkeypatch):
    watched = spy(store, monkeypatch)
    moved = lease.transfer(store, SLUG, "hive", 0, 0)
    assert moved == lease.Lease("hive", 1, NOW + lease.ttl_ms())
    assert json.loads(store.redis.get(store.key(SLUG, "control-owner"))) == {
        "owner": "hive",
        "epoch": 1,
        "expires_at": NOW + lease.ttl_ms(),
    }
    assert 0 < store.redis.pttl(store.key(SLUG, "control-owner")) <= lease.ttl_ms()
    assert watched == [(store.key(SLUG, "control-owner"), store.key(SLUG, "control-epoch"))]


def test_a_transfer_moves_a_live_lease_held_by_its_owner_to_a_higher_epoch(store):
    owner(store, json.dumps({"owner": "hive", "epoch": 4, "expires_at": NOW + 1}))
    store.redis.set(store.key(SLUG, "control-epoch"), 4)
    assert lease.transfer(store, SLUG, "hive", 2, 4).epoch == 5


def test_a_transfer_rises_above_a_live_epoch_ahead_of_the_epoch_counter(store):
    owner(store, json.dumps({"owner": "hive", "epoch": 7, "expires_at": NOW + 1}))
    store.redis.set(store.key(SLUG, "control-epoch"), 4)
    assert lease.transfer(store, SLUG, "hive", 2, 7).epoch == 8


def test_controller_lease_actions_refuse_a_payload_missing_its_fields(store, monkeypatch):
    monkeypatch.setattr(controller_lease, "connect", lambda: store)
    with pytest.raises(APIError) as refused:
        controller_lease.act(SLUG, "", {"action": "renew"})
    assert (refused.value.status, refused.value.code) == (400, "schema_invalid")


def test_a_lease_expiring_now_counts_as_free(store):
    owner(store, json.dumps({"owner": "other", "epoch": 4, "expires_at": NOW}))
    assert lease.transfer(store, SLUG, "hive", 2, 0).epoch == 3


@pytest.mark.parametrize(
    ("raw", "expected", "message"),
    [
        (json.dumps({"owner": "other", "epoch": 4, "expires_at": NOW + 1}), 0, "the controller lease is stale"),
        (json.dumps({"owner": "other", "epoch": 4, "expires_at": NOW + 1}), 4, "the controller lease is stale"),
        ("legacy-owner", 0, "the controller lease belongs to an incompatible owner"),
    ],
)
def test_a_transfer_refuses_a_lease_it_does_not_hold(store, raw, expected, message):
    owner(store, raw)
    with pytest.raises(SwarmError) as refused:
        lease.transfer(store, SLUG, "hive", 2, expected)
    assert str(refused.value) == message


def test_a_transfer_that_keeps_losing_its_watch_is_not_committed(store, monkeypatch):
    watched = spy(store, monkeypatch, fail=True)
    with pytest.raises(SwarmError) as refused:
        lease.transfer(store, SLUG, "hive", 0, 0)
    assert str(refused.value) == "the controller lease kept changing; transfer was not committed"
    assert len(watched) == 5
    assert store.redis.get(store.key(SLUG, "control-owner")) is None
