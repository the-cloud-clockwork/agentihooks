import json
import os
import secrets

import pytest

from scripts.swarm import cli, lease
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_ledger import authority_context, ledger_authority, ledger_route, ledger_server
from scripts.swarm_v2 import masters
from tests.swarm_ledger.test_ledger_authority import SLUG
from tests.swarm_ledger.test_ledger_authority import live as _authority_live

live = _authority_live


@pytest.fixture
def pending(live, monkeypatch, tmp_path):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, str(tmp_path), 1, 0, state="paused"))
    monkeypatch.setattr(lease, "now_ms", lambda *_: 10000)
    store.update(SLUG, pending_api_url=f"http://{live['host']}")
    masters.MasterSeats(store.redis).set_count(SLUG, 2)
    actors = []
    for lane, seat in (("eng", "eng-1"), ("master", masters.seat(1)), ("master", masters.seat(2))):
        name = store.next_name(SLUG, lane)
        actors.append(
            store.start_execution(SLUG, AgentRecord(name, lane, "proof", seat=f"{seat}@{SLUG}", launched_at=10001))
        )
    secret = secrets.token_hex(32)
    monkeypatch.setattr(ledger_server.authority, "environ", {"SWARM_AUTH_MODE": "edge", "SWARM_SECRET": secret})
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda *_: store.redis)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", SLUG)
    monkeypatch.delenv("SWARM_SECRET", raising=False)
    monkeypatch.delenv("SWARM_URL", raising=False)
    return store, actors, secret


def test_readiness_requires_each_current_worker_and_both_masters_to_authenticate(pending, monkeypatch, capsys):
    store, actors, secret = pending

    def report(actor, token):
        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", actor.name)
        monkeypatch.setenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", token)
        assert cli.main([SLUG, "ledger-ready"]) == 0
        return json.loads(capsys.readouterr().out)

    everyone = {actor.name for actor in actors}
    assert set(report(actors[0], "")["missing"]) == everyone
    wrong = ledger_authority.agent_token(secret, "foreign-ledger", actors[0].name)
    assert set(report(actors[0], wrong)["missing"]) == everyone
    for index, actor in enumerate(actors):
        token = ledger_authority.agent_token(secret, SLUG, actor.name)
        status = report(actor, token)
        assert set(status["missing"]) == {remaining.name for remaining in actors[index + 1 :]}
        assert status["ready"] is (index == len(actors) - 1)
    assert store.config(SLUG).api_url == ""
    assert authority_context.secret() is None


def _legacy_master_readiness(
    pending: tuple[RedisStore, list[AgentRecord], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    launched_at: int,
) -> tuple[dict, AgentRecord]:
    """Authenticate every actor and a master in seat three with no execution identity; return the last report."""
    store, actors, secret = pending
    seat = f"{masters.seat(3)}@{SLUG}"
    master = AgentRecord(store.next_name(SLUG, "master"), "master", "proof", seat=seat, launched_at=launched_at)
    store.put_agent(SLUG, master)
    legacy = next(agent for agent in store.agents(SLUG) if agent.seat == seat)
    assert legacy.execution_id == ""

    def report(actor: AgentRecord) -> dict:
        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", actor.name)
        monkeypatch.setenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", ledger_authority.agent_token(secret, SLUG, actor.name))
        assert cli.main([SLUG, "ledger-ready"]) == 0
        return json.loads(capsys.readouterr().out)

    for actor in actors:
        report(actor)
    return report(legacy), legacy


def test_a_master_without_an_execution_launched_after_staging_reads_ready(
    pending: tuple[RedisStore, list[AgentRecord], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status, legacy = _legacy_master_readiness(pending, monkeypatch, capsys, 10001)
    assert status["ready"] is True
    assert status["missing"] == []
    receipt = json.loads(pending[0].redis.hget(pending[0].key(SLUG, "ledger-ready"), legacy.name))
    assert receipt["launched_at"] == 10001
    assert "generation" not in receipt


def test_a_master_without_an_execution_launched_before_staging_stays_missing(
    pending: tuple[RedisStore, list[AgentRecord], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status, legacy = _legacy_master_readiness(pending, monkeypatch, capsys, 9999)
    assert status["ready"] is False
    assert status["missing"] == [legacy.name]


def _finish(store: RedisStore, name: str) -> None:
    """Mark a projected agent row finished, as the tick does when its life ends."""
    row = json.loads(store.redis.hget(store.key(SLUG, "agents"), name))
    store.redis.hset(store.key(SLUG, "agents"), name, json.dumps({**row, "state": "finished"}))


def _acknowledge_as(
    pending: tuple[RedisStore, list[AgentRecord], str], monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Run the ready report as one agent holding its ledger token."""
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", name)
    monkeypatch.setenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", ledger_authority.agent_token(pending[2], SLUG, name))
    assert cli.main([SLUG, "ledger-ready"]) == 0


def _seat_legacy_master(store: RedisStore, launched_at: int = 10001) -> AgentRecord:
    """Put a master with no execution identity in seat three, launched after staging."""
    seat = f"{masters.seat(3)}@{SLUG}"
    master = AgentRecord(store.next_name(SLUG, "master"), "master", "proof", seat=seat, launched_at=launched_at)
    store.put_agent(SLUG, master)
    return next(agent for agent in store.agents(SLUG) if agent.seat == seat)


def test_a_finished_master_without_an_execution_never_authenticates(
    pending: tuple[RedisStore, list[AgentRecord], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import ledger_cutover

    store = pending[0]
    legacy = _seat_legacy_master(store)
    _finish(store, legacy.name)
    calls: list[tuple] = []
    monkeypatch.setattr(ledger_cutover.client.ResourceClient, "request", lambda *args: calls.append(args))
    _acknowledge_as(pending, monkeypatch, legacy.name)
    assert calls == []
    assert store.redis.hget(store.key(SLUG, "ledger-ready"), legacy.name) is None


def test_a_master_without_an_execution_that_finishes_while_authenticating_records_no_receipt(
    pending: tuple[RedisStore, list[AgentRecord], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import ledger_cutover

    store = pending[0]
    legacy = _seat_legacy_master(store)
    real = ledger_cutover.client.ResourceClient.request

    def finishing(
        self: ledger_cutover.client.ResourceClient, slug: str, path: str, payload: dict | None = None
    ) -> dict:
        answer = real(self, slug, path, payload)
        _finish(store, legacy.name)
        return answer

    monkeypatch.setattr(ledger_cutover.client.ResourceClient, "request", finishing)
    _acknowledge_as(pending, monkeypatch, legacy.name)
    assert store.redis.hget(store.key(SLUG, "ledger-ready"), legacy.name) is None


def test_a_master_without_an_execution_removed_while_authenticating_records_no_receipt(
    pending: tuple[RedisStore, list[AgentRecord], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import ledger_cutover

    store = pending[0]
    legacy = _seat_legacy_master(store)
    real = ledger_cutover.client.ResourceClient.request

    def removing(self: ledger_cutover.client.ResourceClient, slug: str, path: str, payload: dict | None = None) -> dict:
        answer = real(self, slug, path, payload)
        store.redis.hdel(store.key(SLUG, "agents"), legacy.name)
        return answer

    monkeypatch.setattr(ledger_cutover.client.ResourceClient, "request", removing)
    _acknowledge_as(pending, monkeypatch, legacy.name)
    assert store.redis.hget(store.key(SLUG, "ledger-ready"), legacy.name) is None


def test_a_master_without_an_execution_launched_at_the_first_millisecond_acknowledges(
    pending: tuple[RedisStore, list[AgentRecord], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = pending[0]
    store.redis.hset(store.key(SLUG, "config"), "ledger_staged_at", 1)
    legacy = _seat_legacy_master(store, launched_at=1)
    _acknowledge_as(pending, monkeypatch, legacy.name)
    receipt = json.loads(store.redis.hget(store.key(SLUG, "ledger-ready"), legacy.name))
    assert receipt == {
        "url": store.config(SLUG).pending_api_url,
        "staged_at": 1,
        "execution_id": "",
        "launched_at": 1,
    }


@pytest.mark.parametrize("command", ["ledger", "swarm"])
def test_normal_local_commands_acknowledge_the_staged_successor(pending, monkeypatch, capsys, command):
    from scripts.swarm_ledger import ledger

    _, actors, secret = pending
    actor = actors[0]
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", actor.name)
    monkeypatch.setenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", ledger_authority.agent_token(secret, SLUG, actor.name))
    if command == "ledger":
        monkeypatch.setattr(ledger, "cmd_url", lambda args: None)
        monkeypatch.setattr("sys.argv", ["ledger", "--slug", SLUG, "url"])
        ledger.main()
    else:
        monkeypatch.setattr(cli, "cmd_status", lambda store, args: None)
        assert cli.main([SLUG, "status"]) == 0
    monkeypatch.setenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", "")
    assert cli.main([SLUG, "ledger-ready"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report["missing"]) == {master.name for master in actors[1:]}
    assert not report["ready"]


def test_endpoint_activation_requires_ready_executions_and_moves_the_controller_lease(pending, monkeypatch, capsys):
    from argparse import Namespace

    import fakeredis

    from scripts.swarm import commands
    from scripts.swarm.store import SwarmError
    from scripts.swarm_ledger.api import controller_lease
    from scripts.swarm_ledger.storage_migration import hosted

    source, actors, secret = pending
    destination = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(controller_lease, "connect", lambda: destination)
    monkeypatch.setattr(ledger_server.authority, "connect", lambda: destination)
    monkeypatch.setattr(hosted, "server_secret", lambda: secret)
    url = source.config(SLUG).pending_api_url
    args = Namespace(slug=SLUG, pairs=[f"api-url={url}"])
    old = lease.acquire(source, SLUG, commands.hive_id())
    with pytest.raises(SwarmError) as unready:
        cli.cmd_set(source, args)
    assert str(unready.value) == "the ledger successors are not ready"
    assert source.config(SLUG).api_url == ""
    assert lease.current(source, SLUG) == old
    assert lease.current(destination, SLUG) is None
    for actor in actors:
        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", actor.name)
        monkeypatch.setenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", ledger_authority.agent_token(secret, SLUG, actor.name))
        assert cli.main([SLUG, "ledger-ready"]) == 0
        capsys.readouterr()
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
    with ledger_route.scoped(SLUG, ""):
        cli.cmd_set(source, args)
        assert ledger_route.url(SLUG) == url
    assert source.config(SLUG).api_url == url
    assert source.redis.hget(source.key(SLUG, "config"), "pending_api_url") == ""
    assert source.redis.hget(source.key(SLUG, "config"), "ledger_staged_at") == "0"
    assert not source.redis.exists(source.key(SLUG, "ledger-ready"))
    assert lease.current(source, SLUG) is None
    assert lease.current(destination, SLUG).epoch > old.epoch
    assert lease.current(destination, SLUG).owner == old.owner
    assert "SWARM_URL" not in os.environ


def test_activation_never_loads_controller_authority_for_a_pinned_caller(pending, monkeypatch):
    from scripts.swarm import ledger_cutover
    from scripts.swarm.store import SwarmError
    from scripts.swarm_ledger import authority_context

    source, actors, _ = pending
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", actors[-1].name)
    monkeypatch.setattr(ledger_cutover, "readiness", lambda *_: {"ready": True})

    def refuse(*_):
        raise AssertionError("caller loaded controller authority")

    monkeypatch.setattr(authority_context, "for_tick", refuse)
    with pytest.raises(SwarmError, match="controller authority"):
        ledger_cutover._activate(source, SLUG, source.config(SLUG).pending_api_url)


def _ready_destination(pending, monkeypatch, capsys):
    import fakeredis

    from scripts.swarm_ledger.api import controller_lease
    from scripts.swarm_ledger.storage_migration import hosted

    source, actors, secret = pending
    destination = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(controller_lease, "connect", lambda: destination)
    monkeypatch.setattr(ledger_server.authority, "connect", lambda: destination)
    monkeypatch.setattr(hosted, "server_secret", lambda: secret)
    for actor in actors:
        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", actor.name)
        monkeypatch.setenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", ledger_authority.agent_token(secret, SLUG, actor.name))
        assert cli.main([SLUG, "ledger-ready"]) == 0
        capsys.readouterr()
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
    return source, destination


def test_a_first_activation_starts_the_hosted_lease_at_epoch_one(pending, monkeypatch, capsys):
    from argparse import Namespace

    source, destination = _ready_destination(pending, monkeypatch, capsys)
    url = source.config(SLUG).pending_api_url
    cli.cmd_set(source, Namespace(slug=SLUG, pairs=[f"api-url={url}"]))
    assert lease.current(destination, SLUG).epoch == 1


def test_an_activation_moves_a_live_hosted_lease_its_controller_already_holds(pending, monkeypatch, capsys):
    from argparse import Namespace

    from scripts.swarm import commands

    source, destination = _ready_destination(pending, monkeypatch, capsys)
    held = {"owner": commands.hive_id(), "epoch": 3, "expires_at": 10000 + lease.ttl_ms()}
    destination.redis.set(destination.key(SLUG, "control-owner"), json.dumps(held))
    destination.redis.set(destination.key(SLUG, "control-epoch"), 3)
    url = source.config(SLUG).pending_api_url
    cli.cmd_set(source, Namespace(slug=SLUG, pairs=[f"api-url={url}"]))
    assert lease.current(destination, SLUG).epoch == 4
    assert source.config(SLUG).api_url == url


def test_an_activation_that_loses_its_commit_releases_the_moved_lease(pending, monkeypatch, capsys):
    from argparse import Namespace

    from scripts.swarm import ledger_cutover
    from scripts.swarm.store import SwarmError

    source, destination = _ready_destination(pending, monkeypatch, capsys)
    url = source.config(SLUG).pending_api_url
    moved = []
    transfer = lease.transfer

    def interrupted(*args):
        moved.append(transfer(*args))
        source.redis.hset(source.key(SLUG, "config"), "touched", "1")
        return moved[-1]

    monkeypatch.setattr(lease, "transfer", interrupted)
    with pytest.raises(SwarmError) as refused:
        cli.cmd_set(source, Namespace(slug=SLUG, pairs=[f"api-url={url}"]))
    assert str(refused.value) == "the ledger activation changed before commit"
    assert moved and lease.current(destination, SLUG) is None
    assert source.config(SLUG).api_url == ""
    assert source.redis.hgetall(source.key(SLUG, ledger_cutover.READY))
