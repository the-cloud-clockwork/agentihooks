import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit

PREDECESSOR = {"profile": "engineer", "harness": "claude", "model": "opus", "effort": "high", "account": "old"}


def _account(name, sessions=0, cap=3, state="OPEN"):
    from scripts.swarm.capacity import Account

    return Account("claude", name, state, sessions, 80, 80, cap)


@pytest.fixture
def hive(monkeypatch, tmp_path):
    import fakeredis

    from scripts.hive.orders import Consumer, Orders
    from scripts.swarm import lease, runtime
    from scripts.swarm.store import RedisStore, SwarmConfig
    from scripts.swarm_v2.runtime.local import LocalHerdrRuntime

    redis = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(redis)
    store.create(SwarmConfig("fixture", str(tmp_path), 4, 0))
    held = lease.acquire(store, "fixture", "controller")
    rows = [_account("old", state="DRAIN", cap=0), _account("admitted"), _account("spare")]
    herdr = runtime.HerdrRuntime(home=tmp_path)
    herdr.quota_accounts = lambda environ, now, *, refresh: list(rows)
    launched = []

    def launch(config, lane, task, name, argv, **kwargs):
        account = argv[argv.index("--route") + 1]
        launched.append(account)
        return runtime.Placed(
            f"pane-{len(launched)}",
            "claude",
            account,
            profile="engineer",
            profile_decision={"validation": {"pid": 12}},
        )

    monkeypatch.setattr(herdr, "_launch", launch)
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda _: False)
    monkeypatch.setattr(runtime.prompt, "build", lambda *args, **kwargs: "opening")
    adapter = LocalHerdrRuntime(herdr)
    adapter.namespace = lambda: "namespace"
    adapter.table = lambda: {12: SimpleNamespace(start_time=34)}
    adapter.terminate = Mock()
    return SimpleNamespace(
        redis=redis,
        store=store,
        held=held,
        rows=rows,
        launched=launched,
        adapter=adapter,
        orders=Orders(redis),
        consumer=Consumer(redis, "host", adapter),
    )


def _order(hive, number, reason, slot="admitted"):
    from scripts.hive.orders import LaunchOrder, carried
    from scripts.swarm import lease
    from scripts.swarm.store import AgentRecord

    task = f"task-{number}"
    agent = hive.store.start_execution(
        "fixture",
        AgentRecord(
            hive.store.next_name("fixture", "eng"),
            "eng",
            task,
            seat=f"eng-{number}@fixture",
            state="launching",
            harness="claude",
            profile="engineer",
            account=slot,
            hive="host",
            runtime_target={"host_id": "host"},
        ),
    )
    carried_task = {
        "id": task,
        "title": "Continue",
        "handoff": "Saved Handoff v2",
        "handoff_envelope": {"reason": reason, "launch": PREDECESSOR},
    }
    prompt = f"prompt-{number}"
    hive.redis.set(prompt, json.dumps(carried("fixture", carried_task, "opening")))
    return LaunchOrder(
        "fixture",
        agent.execution_id,
        agent.generation,
        "engineer",
        "eng",
        task,
        slot,
        "claude",
        "subscription",
        prompt,
        hive.held.epoch,
        lease.now_ms(hive.store) + 60000,
        "host",
    )


@pytest.mark.parametrize(
    "reasons",
    [("quota",), ("quota", "quota"), ("quota", "quota", "quota"), ("quota", "recycle"), ("quota", "quota", "recycle")],
)
def test_an_admitted_quota_successor_launches_on_exactly_its_admitted_account(hive, reasons):
    orders = [_order(hive, number, reason) for number, reason in enumerate(reasons)]
    for order in orders:
        hive.orders.send("host", order)
    hive.consumer.poll()

    acks = [hive.orders.ack(order) for order in orders]
    assert [(ack.status, ack.failure) for ack in acks] == [("live", "")] * len(orders)
    assert hive.launched == ["admitted"] * len(orders)
    hive.adapter.terminate.assert_not_called()


def test_an_unusable_admitted_account_is_refused_for_readmission_without_a_launch(hive):
    hive.rows[1] = _account("admitted", sessions=3)
    order = _order(hive, 0, "quota")
    hive.orders.send("host", order)
    hive.consumer.poll()

    ack = hive.orders.ack(order)
    assert ack.status == "failed"
    assert ack.failure.startswith("unavailable")
    assert "admitted" in ack.failure
    assert hive.launched == []
    hive.adapter.terminate.assert_not_called()
