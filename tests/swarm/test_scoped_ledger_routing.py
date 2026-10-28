import json
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from scripts.swarm import cli, lease
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm_ledger import authority_context, ledger, ledger_link, ledger_route
from tests.swarm.test_cli import env as env
from tests.swarm.test_cli import run


def test_set_stores_the_swarm_api_address_and_clears_it(env, capsys, monkeypatch):
    from scripts.swarm import ledger_cutover

    store, _, _ = env
    monkeypatch.setattr(ledger_cutover, "settings", lambda store, slug, changes: store.update(slug, **changes))
    run("sw", "create", "--repo", "/repo")
    assert store.config("sw").api_url == ""
    assert run("sw", "set", "api-url=https://swarm.example.test:8443") == 0
    assert store.config("sw").api_url == "https://swarm.example.test:8443"
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["api_url"] == "https://swarm.example.test:8443"
    assert run("sw", "set", "api-url=http://swarm-api.agentihooks-swarm.svc:8780") == 0
    assert store.config("sw").api_url == "http://swarm-api.agentihooks-swarm.svc:8780"
    assert run("sw", "set", "api-url=") == 0
    assert store.config("sw").api_url == ""


@pytest.fixture
def store():
    import fakeredis

    result = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    result.create(SwarmConfig("selected", "/repo", 0, 0, api_url="https://hosted.example"))
    result.create(SwarmConfig("other", "/repo", 0, 0))
    return result


def test_only_the_selected_tick_uses_the_hosted_ledger(store, monkeypatch):
    seen = {}
    before = dict(os.environ)
    monkeypatch.setattr(cli.outbox, "flush", lambda *_: None)
    monkeypatch.setattr(cli.relay, "relay", lambda *_: None)
    monkeypatch.setattr(cli.LaunchSlots, "refresh_reported", lambda *_: None)
    monkeypatch.setattr(cli.outage, "providers", lambda *_: None)
    monkeypatch.setattr(cli, "_publish_tick_status", lambda *_: None)
    monkeypatch.setattr(lease, "acquire", lambda *_: lease.Lease("host", 4, 10**15))
    from scripts.swarm import controller

    monkeypatch.setattr(controller, "take_tick_lock", lambda *_: "lock")
    monkeypatch.setattr(controller, "keep_tick", lambda *_: None)
    monkeypatch.setattr(controller, "release_tick_lock", lambda *_: None)

    def probe(slug, _now, _check):
        seen[slug] = (ledger.base(), ledger_link.remote())
        return False

    monkeypatch.setattr(cli.outage, "ledger_health", probe)
    with authority_context.bound("trusted-tick-fixture"), ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda slug: cli.run_tick(store, slug, runtime=object()), ("selected", "other")))
    assert results == [["Ledger health probe failed"], ["Ledger health probe failed"]]
    assert seen == {
        "selected": ("https://hosted.example", True),
        "other": (ledger.BASE or ledger_link.base(), False),
    }
    assert dict(os.environ) == before


def test_a_pinned_native_ledger_command_resolves_its_own_swarm(store, monkeypatch):
    seen = []
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "selected")
    monkeypatch.setattr("scripts.swarm.store.connect", lambda: store)
    monkeypatch.setattr(ledger, "cmd_url", lambda args: seen.append((args.slug, ledger.base(), ledger_link.remote())))
    with patch("sys.argv", ["ledger", "--slug", "selected", "url"]):
        ledger.main()
    assert seen == [("selected", "https://hosted.example", True)]
    assert not ledger_link.remote()


def test_a_native_remote_command_never_opens_a_workstation_store(monkeypatch):
    seen = []
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "selected")
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "compose")
    monkeypatch.setenv("LEDGER_URL", "https://hosted.example")
    monkeypatch.delenv("SWARM_URL", raising=False)
    monkeypatch.setattr(ledger, "BASE", "")
    monkeypatch.setattr(ledger, "cmd_url", lambda args: seen.append(ledger.base()))

    def refuse():
        raise AssertionError("remote command opened the workstation store")

    monkeypatch.setattr("scripts.swarm.store.connect", refuse)
    with patch("sys.argv", ["ledger", "--slug", "selected", "url"]):
        ledger.main()
    assert seen == ["https://hosted.example"]


def test_scoped_routes_match_the_slug_and_restore_after_a_refusal():
    assert ledger_route.url("selected") == ""
    with ledger_route.scoped("selected", "https://hosted.example/"):
        assert ledger_route.url("selected") == "https://hosted.example"
        assert ledger_route.url("other") == ""
        assert ledger.base("selected") == "https://hosted.example"
        assert not ledger_link.remote(slug="other")
        with pytest.raises(ValueError):
            with ledger_route.scoped("other", "https://other.example"):
                assert ledger_route.url("selected") == ""
                raise ValueError("refused")
        assert ledger_route.url("selected") == "https://hosted.example"
    assert ledger_route.url() == ""


def test_an_active_successor_keeps_an_unrelated_swarm_local(store, monkeypatch):
    from scripts.gates import Who

    monkeypatch.setenv("AGENTIHOOKS_SWARM", "selected")
    monkeypatch.setenv("SWARM_URL", "https://hosted.example")
    who = Who(name="engineer@abcdef-0001", swarm="selected")
    with ledger_route.command(store, "other", who):
        assert not ledger_link.remote(slug="other")
        assert ledger.base("other") != "https://hosted.example"
    with ledger_route.command(store, "selected", who):
        assert ledger_link.remote(slug="selected")
        assert ledger.base("selected") == "https://hosted.example"


def test_pending_destination_stages_private_authority_without_routing_the_tick(store, monkeypatch):
    from argparse import Namespace

    from scripts import operator_env
    from scripts.swarm_ledger import authority_context
    from scripts.swarm_ledger.storage_migration import hosted

    store.update("selected", api_url="")
    monkeypatch.setattr(cli, "run_tick", lambda *_: [])
    cli.cmd_set(store, Namespace(slug="selected", pairs=["api-url-pending=https://hosted.example"]))
    config = store.config("selected")
    assert config.api_url == ""
    assert config.pending_api_url == "https://hosted.example"
    assert config.ledger_staged_at > 0
    assert store.config("other").pending_api_url == ""
    seen = []
    monkeypatch.setattr(cli.timer, "installed_refusal", lambda: "")
    monkeypatch.setattr(operator_env, "fill", lambda *_: None)
    monkeypatch.setattr(operator_env, "classifier", lambda *_: None)
    monkeypatch.setattr(hosted, "server_secret", lambda: "private-staging-fixture")
    monkeypatch.setattr(cli, "_reconcile_services", lambda: None)

    def run(slugs, callback, settled, elapsed):
        seen.append((authority_context.secret(), ledger_link.remote()))
        return True

    monkeypatch.setattr(cli.tick_stop, "run", run)
    cli.cmd_tick(store, None)
    assert seen == [("private-staging-fixture", False)]
    assert "SWARM_SECRET" not in os.environ
    assert authority_context.secret() is None


def test_timer_prechecks_and_waits_resolve_the_selected_route(store, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_tickable", lambda slug: seen.append((slug, ledger.base())) or False)
    cli._tick_one(store, "selected")
    cli._tick_one(store, "other")
    assert seen == [("selected", "https://hosted.example"), ("other", ledger.BASE or ledger_link.base())]
    seen.clear()
    monkeypatch.setattr(lease, "acquire", lambda _store, slug, owner: seen.append((slug, ledger.base())))
    with authority_context.bound("trusted-tick-fixture"):
        assert cli.run_waits(store, "selected") == []
        assert cli.run_waits(store, "other") == []
    assert seen == [("selected", "https://hosted.example"), ("other", ledger.BASE or ledger_link.base())]


@pytest.mark.parametrize("active", [False, True])
def test_an_agent_control_leaves_the_hosted_service_tick_to_its_trusted_runner(store, monkeypatch, active):
    from types import SimpleNamespace

    from scripts.swarm_ledger import authority_context

    monkeypatch.setattr(cli.Who, "from_env", lambda: SimpleNamespace(pinned=True, name="master@a1b2c3-0001"))
    store.update(
        "selected",
        api_url="https://hosted.example" if active else "",
        pending_api_url="https://hosted.example",
    )
    acquired = []
    monkeypatch.setattr(lease, "acquire", lambda *_: acquired.append(True))
    with authority_context.bound(None):
        actions = cli.run_tick(store, "selected", runtime=object())
    assert not acquired
    assert actions


@pytest.mark.parametrize("deployment", ["distributed", "compose"])
def test_a_controller_deployment_ticks_its_swarm_on_its_own_ledger_link(store, monkeypatch, deployment):
    from scripts.swarm import commands
    from scripts.swarm_ledger import authority_context

    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", deployment)
    monkeypatch.setattr(commands, "hive_id", lambda: "kind")
    monkeypatch.setenv("LEDGER_URL", "https://ledger.kind.test:8765")
    monkeypatch.delenv("SWARM_URL", raising=False)
    monkeypatch.setattr(ledger, "BASE", "")
    monkeypatch.setattr(cli.outbox, "flush", lambda *_: None)
    acquired = []
    monkeypatch.setattr(lease, "acquire", lambda _store, slug, owner: acquired.append((slug, ledger.base(slug))))
    with authority_context.bound(None):
        actions = cli.run_tick(store, "selected", runtime=object(), scheduled=True)
    assert actions == ["the swarm belongs to another hive"]
    assert acquired == [("selected", "https://ledger.kind.test:8765")]
    assert ledger_route.configured(store, "selected") == ""


def test_a_native_control_notice_uses_its_callers_own_authority(store, monkeypatch):
    from types import SimpleNamespace

    from scripts.swarm import ledger_client
    from scripts.swarm_ledger import ledger_authority

    master = "master@a1b2c3-0001"
    monkeypatch.setattr(cli.Who, "from_env", lambda: SimpleNamespace(pinned=True, name=master))
    delivered = []

    def call(slug, ops, service):
        assert slug == "selected"
        assert not ledger_authority.refusal(master, ops[0])
        assert not service
        delivered.append(ops[0]["text"])
        return {}

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=call, Missing=ledger.Missing))
    with ledger_route.scoped("selected", store.config("selected").api_url):
        ledger_client.LedgerClient().notify("selected", "The master paused the swarm")
    assert delivered == ["The master paused the swarm"]


HOSTED = "https://hosted.example"
LOCAL = "http://127.0.0.1:8765"


@pytest.fixture
def local_env(monkeypatch):
    for name in ("SWARM_URL", "LEDGER_URL", "AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_CONTROLLER_CREDENTIAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    monkeypatch.setattr(ledger_link, "address", lambda environ=None: ("127.0.0.1", 8765))
    monkeypatch.setattr(ledger_link, "scheme", lambda environ=None: "http")


@pytest.fixture
def clients(local_env, monkeypatch):
    from scripts.swarm_ledger.api import client

    seen = []

    class Recorder:
        def __init__(self, base, headers, timeout=None):
            seen.append(base)

        def snapshot(self, slug):
            return {"snapshot": slug}

        def mutate(self, slug, ops):
            return {"mutated": slug}

        def request(self, slug, path, payload=None):
            return {"data": {"token": "minted"} if path == "agent-token" else {"path": path}}

    monkeypatch.setattr(client, "ResourceClient", Recorder)
    monkeypatch.setattr(ledger, "BASE", "http://local.test")
    monkeypatch.setattr(ledger, "credentials", lambda slug, service=False: {})
    monkeypatch.setattr(ledger, "local_credentials", lambda *_: {})
    monkeypatch.setenv("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", "hive")
    return Recorder, seen


def test_ledger_reads_and_writes_for_another_swarm_stay_local_while_one_is_routed(clients):
    _, seen = clients
    with ledger_route.scoped("selected", HOSTED):
        assert ledger.request("other") == {"snapshot": "other"}
        assert ledger.request("other", [{"op": "join"}]) == {"mutated": "other"}
        assert ledger.replay("other", [{"op": "join"}], "", False) == {"mutated": "other"}
        assert ledger.resource("other", "metadata") == {"path": "metadata"}
        assert ledger.export("other") == {"path": "export"}
        assert ledger.launch_token("other", "engineer@a1b2c3-0001") == "minted"
    assert seen == ["http://local.test"] * 6


def test_a_failed_local_read_for_another_swarm_is_not_retried(clients, monkeypatch):
    recorder, _ = clients
    attempts = []

    def fail(self, slug, path, payload=None):
        attempts.append(path)
        raise OSError("ledger down")

    monkeypatch.setattr(recorder, "request", fail)
    monkeypatch.setattr(ledger, "REMOTE_READ_PAUSE", 0)
    with ledger_route.scoped("selected", HOSTED):
        with pytest.raises(OSError):
            ledger.resource("other", "metadata")
        with pytest.raises(OSError):
            ledger.export("other")
    assert attempts == ["metadata", "export"]


def test_credentials_and_calls_for_another_swarm_stay_local_while_one_is_routed(local_env, monkeypatch):
    class LocalPath(Exception):
        pass

    def local(ops):
        raise LocalPath

    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.setattr(ledger, "local_credentials", lambda slug, agent, service: {"local": slug})
    monkeypatch.setattr(ledger, "talk", local)
    monkeypatch.setattr(ledger, "remote_call", lambda *_: "remote")
    with ledger_route.scoped("selected", HOSTED):
        assert ledger.credentials("other") == {"local": "other"}
        with pytest.raises(LocalPath):
            ledger.call("other", [])


def test_the_ledger_base_asks_the_link_for_its_own_swarm(monkeypatch):
    monkeypatch.setattr(ledger, "BASE", "")
    monkeypatch.setattr(ledger.ledger_link, "base", lambda environ=None, slug="": f"http://link.test/{slug}")
    assert ledger.base("other") == "http://link.test/other"


def test_link_addresses_route_only_their_own_swarm(local_env):
    environ = {"AGENTIHOOKS_DEPLOYMENT": "local", "AGENTIHOOKS_SWARM": "selected", "SWARM_URL": "https://env.example"}
    assert ledger_link.base(environ, slug="other") == LOCAL
    assert ledger_link.base(environ, slug="selected") == "https://env.example"
    with ledger_route.scoped("selected", HOSTED):
        assert ledger_route.url() == HOSTED
        assert ledger_link.base({"AGENTIHOOKS_DEPLOYMENT": "local"}) == HOSTED
        assert ledger_link.base({"AGENTIHOOKS_DEPLOYMENT": "local"}, slug="other") == LOCAL
        assert ledger_link.page_url("other") == f"{LOCAL}/other"
        assert ledger_link.page_url("selected") == f"{HOSTED}/selected"


def test_lifecycle_actions_for_another_swarm_stay_local_while_one_is_routed(local_env, monkeypatch):
    import sys
    from types import SimpleNamespace

    from scripts.gates import Who
    from scripts.swarm import ledger_client

    done, ops = [], []
    bins = SimpleNamespace(
        restore=lambda slug: done.append(("restore", slug)),
        bin_closed=lambda slug, closed_at: ("closed", slug, closed_at),
        entries=lambda: ["other"],
    )
    monkeypatch.setitem(sys.modules, "ledger_bin", bins)
    monkeypatch.setattr(ledger_client, "_ledger", lambda: None)
    monkeypatch.setattr(Who, "from_env", lambda: SimpleNamespace(pinned=True, name="master@a1b2c3-0001"))
    client = ledger_client.LedgerClient()
    monkeypatch.setattr(client, "_call", lambda slug, batch: ops.extend(batch))

    def hosted(*_):
        raise AssertionError("a lifecycle action went to the hosted ledger")

    monkeypatch.setattr(client, "_lifecycle", hosted)
    with ledger_route.scoped("selected", HOSTED):
        client.reopen("other", "operator")
        assert client.bin_closed("other", 5) == ("closed", "other", 5)
        assert client.binned("other") is True
        client.notify("other", "The master paused the swarm")
    assert done == [("restore", "other")]
    assert [op["by"] for op in ops] == ["operator", "swarm"]


def test_a_lifecycle_request_uses_its_own_swarm_address(monkeypatch):
    from types import SimpleNamespace

    from scripts.swarm import ledger_client
    from scripts.swarm_ledger.api import client

    seen = []

    class Recorder:
        def __init__(self, base, headers, timeout=None):
            seen.append(base)

        def request(self, slug, path, payload):
            return {"data": (slug, path, payload)}

    fake = SimpleNamespace(base=lambda slug: f"base:{slug}", credentials=lambda slug, service: {})
    monkeypatch.setattr(ledger_client, "_ledger", lambda: fake)
    monkeypatch.setattr(client, "ResourceClient", Recorder)
    result = ledger_client.LedgerClient()._lifecycle("other", {"action": "binned"})
    assert result == ("other", "lifecycle/actions", {"action": "binned"})
    assert seen == ["base:other"]


def test_a_swarm_command_runs_inside_its_own_route_and_reports_its_master(store, monkeypatch):
    from scripts.swarm import control_notifications

    seen = {}
    lead = object()
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "cmd_status", lambda store, args: seen.update(route=ledger_route.url("selected")))
    monkeypatch.setattr(control_notifications, "CONTROLS", {"status": "showed the status"})
    monkeypatch.setattr(control_notifications, "master", lambda store, slug: lead)

    def notify(store, args, ledger, before, action):
        seen.update(before=before, action=action)

    monkeypatch.setattr(control_notifications, "notify", notify)
    assert cli.main(["selected", "status"]) == 0
    assert seen == {"route": HOSTED, "before": lead, "action": "status"}


def test_staging_fields_default_for_a_record_without_them_and_clear_with_the_destination(store, monkeypatch):
    store.redis.hdel(store.key("other", "config"), "pending_api_url", "ledger_staged_at")
    config = store.config("other")
    assert (config.pending_api_url, config.ledger_staged_at) == ("", 0)
    monkeypatch.setattr(lease, "now_ms", lambda *_: 4321)
    assert store.update("other", pending_api_url=HOSTED).ledger_staged_at == 4321
    assert store.update("other", pending_api_url="").ledger_staged_at == 0
