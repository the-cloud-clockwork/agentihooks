from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


@pytest.fixture
def consumer(monkeypatch):
    import fakeredis

    monkeypatch.delenv("SWARM_SECRET", raising=False)
    redis = fakeredis.FakeRedis(decode_responses=True)
    seen = []

    def stream(hive):
        assert hive == "host"
        return "orders:host"

    def poll():
        from scripts.swarm_ledger import authority_context

        seen.append(authority_context.secret())

    return SimpleNamespace(
        store=SimpleNamespace(redis=redis), orders=SimpleNamespace(stream=stream), hive="host", poll=poll, seen=seen
    )


def hosted_calls(monkeypatch, value="hosted-secret"):
    from scripts.swarm_ledger import authority_context

    calls = []
    monkeypatch.setattr(authority_context, "hosted", lambda: calls.append(1) or value)
    return calls


def test_a_workstation_hive_applies_pending_orders_with_the_hosted_ledger_secret(monkeypatch, consumer):
    from scripts.hive import daemon
    from scripts.swarm_ledger import authority_context

    calls = hosted_calls(monkeypatch)
    consumer.store.redis.xadd("orders:host", {"order": "{}"})

    daemon.poll_orders(consumer, member=False)

    assert consumer.seen == ["hosted-secret"]
    assert calls == [1]
    assert authority_context.secret() is None


def test_a_member_hive_never_reads_the_hosted_ledger_secret(monkeypatch, consumer):
    from scripts.hive import daemon

    calls = hosted_calls(monkeypatch)
    consumer.store.redis.xadd("orders:host", {"order": "{}"})

    daemon.poll_orders(consumer, member=True)

    assert consumer.seen == [None]
    assert calls == []


def test_an_empty_order_stream_reads_no_secret(monkeypatch, consumer):
    from scripts.hive import daemon

    calls = hosted_calls(monkeypatch)

    daemon.poll_orders(consumer, member=False)

    assert consumer.seen == [None]
    assert calls == []


def test_a_set_server_secret_still_mints_without_a_hosted_ledger(monkeypatch, consumer):
    from scripts.hive import daemon

    hosted_calls(monkeypatch, value=None)
    monkeypatch.setenv("SWARM_SECRET", "local-secret")
    consumer.store.redis.xadd("orders:host", {"order": "{}"})

    daemon.poll_orders(consumer, member=False)

    assert consumer.seen == ["local-secret"]


def test_hosted_reads_the_server_secret_only_with_a_hosted_ledger_address(monkeypatch):
    from scripts.swarm_ledger import authority_context
    from scripts.swarm_ledger.storage_migration import hosted

    monkeypatch.setattr(hosted, "server_secret", lambda: "from-openbao")
    monkeypatch.delenv("AGENTIHOOKS_HOSTED_LEDGER_URL", raising=False)
    assert authority_context.hosted() is None

    monkeypatch.setenv("AGENTIHOOKS_HOSTED_LEDGER_URL", "https://ledger.example")
    assert authority_context.hosted() == "from-openbao"


def test_applying_orders_keeps_the_daemon_server_secret_setting(monkeypatch, consumer):
    import os

    from scripts.hive import daemon

    hosted_calls(monkeypatch)
    monkeypatch.setenv("SWARM_SECRET", "local-secret")
    consumer.store.redis.xadd("orders:host", {"order": "{}"})

    daemon.poll_orders(consumer, member=False)

    assert consumer.seen == ["hosted-secret"]
    assert os.environ["SWARM_SECRET"] == "local-secret"


def test_an_agent_launched_while_orders_apply_carries_its_ledger_agent_token(monkeypatch, consumer):
    from scripts.hive import daemon
    from scripts.swarm import runtime
    from scripts.swarm_ledger.ledger_authority import agent_token

    hosted_calls(monkeypatch)
    consumer.store.redis.xadd("orders:host", {"order": "{}"})
    launched = []
    consumer.poll = lambda: launched.append(runtime._ledger_token("fixture", "engineer@1-1"))

    daemon.poll_orders(consumer, member=False)

    assert launched == [{"AGENTIHOOKS_LEDGER_AGENT_TOKEN": agent_token("hosted-secret", "fixture", "engineer@1-1")}]
    assert runtime._ledger_token("fixture", "engineer@1-1") == {}
