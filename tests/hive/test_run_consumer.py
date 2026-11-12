from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

REPOSITORY = "github.com/owner/project"
WORKFLOW = (
    "workflow:\n"
    "  name: project-only\n"
    "  intent: Build it.\n"
    "  head: build\n"
    "  exit: build.pass\n"
    "stages:\n"
    "  build:\n"
    "    profile: engineer\n"
    "    next:\n"
    "      pass: exit\n"
)


class Start:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str, str, str]] = []

    def __call__(self, name: str, case: str, project: str, delivery_id: str, owner: str) -> bool:
        self.calls.append((name, case, Path(case).read_text(), project, delivery_id, owner))
        return True


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any, Start, Path]:
    import fakeredis

    from hooks import config
    from scripts.hive import run_consumer, run_orders
    from scripts.workflows import loader

    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(loader.profile_chain, "read_state", lambda: {})
    root = tmp_path / "dev"
    project = root / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".git" / "config").write_text('[remote "origin"]\n\turl = git@github.com:owner/project.git\n')
    (project / loader.PROJECT_WORKFLOWS).mkdir(parents=True)
    (project / loader.PROJECT_WORKFLOWS / "project-only.yaml").write_text(WORKFLOW)
    monkeypatch.setenv("HIVE_REPO_ROOT", str(root))
    monkeypatch.setattr(run_consumer.time, "time", lambda: 1000.0)
    start = Start()
    monkeypatch.setattr(run_consumer.scheduled, "start", start)
    redis = fakeredis.FakeRedis(decode_responses=True)
    return redis, run_consumer, run_orders, start, project


def order(run_orders: Any, **fields: Any) -> Any:
    chosen = {
        "run_id": "run-1",
        "workflow": "project-only",
        "case": "case text",
        "repository": REPOSITORY,
        "owner": "owner@fixture",
        "needs": ("build",),
        "hive": "host",
        "expires_at": 2_000_000,
        **fields,
    }
    return run_orders.RunOrder(**chosen)


@pytest.mark.feature("hive-headless-run-consumer")
def test_a_run_order_starts_one_detached_run_in_the_project_checkout(setup: tuple[Any, ...]) -> None:
    from scripts.hive.orders import Ack

    redis, run_consumer, run_orders, start, project = setup
    store = run_orders.RunOrders(redis)
    sent = order(run_orders)
    store.send("host", sent)
    assert run_consumer.RunConsumer(redis, "host").poll() == 1
    [(name, case, text, folder, run_id, owner)] = start.calls
    assert (name, text, folder, run_id, owner) == ("project-only", "case text", str(project), "run-1", "owner@fixture")
    assert Path(case) == run_consumer.case_file("run-1")
    assert store.ack(sent) == Ack("done", {"host_id": "host"})
    assert redis.hget(store.receipt(sent), "started") == "1000000"
    assert redis.xlen(store.stream("host")) == 0


@pytest.mark.feature("hive-headless-run-consumer")
def test_a_poll_starts_one_batch_of_run_orders(setup: tuple[Any, ...]) -> None:
    redis, run_consumer, run_orders, start, _ = setup
    store = run_orders.RunOrders(redis)
    for index in range(run_consumer.BATCH + 1):
        store.send("host", order(run_orders, run_id=f"run-{index}"))
    assert run_consumer.RunConsumer(redis, "host").poll() == run_consumer.BATCH
    assert len(start.calls) == run_consumer.BATCH
    assert redis.xlen(store.stream("host")) == 1


@pytest.mark.feature("hive-headless-run-consumer")
def test_a_run_order_is_claimed_once_by_its_receipt(setup: tuple[Any, ...]) -> None:
    redis, run_consumer, run_orders, start, _ = setup
    store = run_orders.RunOrders(redis)
    sent = order(run_orders)
    store.send("host", sent)
    redis.hset(store.receipt(sent), "started", "1")
    assert run_consumer.RunConsumer(redis, "host").poll() == 1
    assert start.calls == []
    assert store.ack(sent) is None
    assert redis.xlen(store.stream("host")) == 1
    redis.hdel(store.receipt(sent), "started")
    run_consumer.RunConsumer(redis, "host").poll()
    redis.xadd(store.stream("host"), {"order": sent.encode()})
    run_consumer.RunConsumer(redis, "host").poll()
    assert len(start.calls) == 1
    assert redis.xlen(store.stream("host")) == 0


@pytest.mark.parametrize(
    ("fields", "failure"),
    [
        ({"expires_at": 1_000_000}, "expired"),
        ({"repository": "github.com/owner/missing"}, "unknown_repository"),
        ({"workflow": "absent"}, "missing_workflow"),
    ],
)
@pytest.mark.feature("hive-headless-run-consumer")
def test_a_run_order_that_cannot_start_acks_failed_with_its_reason(
    setup: tuple[Any, ...], fields: dict[str, Any], failure: str
) -> None:
    from scripts.hive.orders import Ack

    redis, run_consumer, run_orders, start, _ = setup
    store = run_orders.RunOrders(redis)
    sent = order(run_orders, **fields)
    store.send("host", sent)
    run_consumer.RunConsumer(redis, "host").poll()
    assert start.calls == []
    assert store.ack(sent) == Ack("failed", {}, failure)
    assert redis.xlen(store.stream("host")) == 0


@pytest.mark.feature("hive-headless-run-consumer")
def test_a_claimed_run_order_past_its_expiry_acks_expired(setup: tuple[Any, ...]) -> None:
    from scripts.hive.orders import Ack

    redis, run_consumer, run_orders, start, _ = setup
    store = run_orders.RunOrders(redis)
    sent = order(run_orders, expires_at=1_000_000)
    store.send("host", sent)
    redis.hset(store.receipt(sent), "started", "1")
    run_consumer.RunConsumer(redis, "host").poll()
    assert start.calls == []
    assert store.ack(sent) == Ack("failed", {}, "expired")


@pytest.mark.feature("hive-headless-run-consumer")
def test_a_run_order_whose_start_fails_acks_start_failed(
    setup: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hive.orders import Ack

    redis, run_consumer, run_orders, _, _ = setup

    def broken(name: str, case: str, project: str, delivery_id: str, owner: str) -> bool:
        raise OSError("spawn failed")

    monkeypatch.setattr(run_consumer.scheduled, "start", broken)
    store = run_orders.RunOrders(redis)
    sent = order(run_orders)
    store.send("host", sent)
    assert run_consumer.RunConsumer(redis, "host").poll() == 1
    assert store.ack(sent) == Ack("failed", {}, "start_failed")
    assert redis.xlen(store.stream("host")) == 0


@pytest.mark.parametrize(
    ("raw", "failure"),
    [
        ("not json", "invalid_order"),
        ('{"run_id": "run-1"}', "invalid_order"),
        ('"x"', "invalid_order"),
        ("null", "invalid_order"),
    ],
)
@pytest.mark.feature("hive-headless-run-consumer")
def test_an_unreadable_run_order_is_dropped_as_invalid(setup: tuple[Any, ...], raw: str, failure: str) -> None:
    from scripts.hive.orders import Ack

    redis, run_consumer, run_orders, start, _ = setup
    store = run_orders.RunOrders(redis)
    item = redis.xadd(store.stream("host"), {"order": raw})
    run_consumer.RunConsumer(redis, "host").poll()
    assert start.calls == []
    assert redis.hget(f"{store.stream('host')}:acks", item) == Ack("failed", {}, failure).encode()
    assert redis.xlen(store.stream("host")) == 0


@pytest.mark.feature("hive-headless-run-consumer")
def test_a_run_order_naming_an_unsafe_run_id_is_dropped_as_invalid(setup: tuple[Any, ...]) -> None:
    redis, run_consumer, run_orders, start, _ = setup
    store = run_orders.RunOrders(redis)
    item = redis.xadd(store.stream("host"), {"order": order(run_orders, run_id="../escape").encode()})
    run_consumer.RunConsumer(redis, "host").poll()
    assert start.calls == []
    assert "invalid_order" in redis.hget(f"{store.stream('host')}:acks", item)


@pytest.mark.feature("hive-headless-run-consumer")
def test_a_run_order_for_another_hive_starts_nothing(setup: tuple[Any, ...]) -> None:
    redis, run_consumer, run_orders, start, _ = setup
    store = run_orders.RunOrders(redis)
    item = redis.xadd(store.stream("host"), {"order": order(run_orders, hive="other").encode()})
    run_consumer.RunConsumer(redis, "host").poll()
    assert start.calls == []
    assert "identity" in redis.hget(f"{store.stream('host')}:acks", item)
    assert redis.xlen(store.stream("host")) == 0


@pytest.mark.parametrize(("fenced", "draining", "polled"), [(False, False, 1), (True, False, 0), (False, True, 0)])
@pytest.mark.feature("hive-headless-run-consumer")
def test_a_daemon_beat_polls_run_orders_only_on_a_hive_neither_fenced_nor_draining(
    setup: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch, fenced: bool, draining: bool, polled: int
) -> None:
    from scripts.hive import daemon, registry

    redis, run_consumer, _, _, _ = setup
    polls: list[str] = []
    monkeypatch.setattr(daemon, "timer_state", lambda waiter: waiter)
    monkeypatch.setattr(
        daemon, "observe", lambda **kwargs: {"slots": [], "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    )
    monkeypatch.setattr(daemon, "fence", lambda redis, local_id, runtime: fenced)
    monkeypatch.setattr(registry, "member_draining", lambda client, hive_id, member: draining)
    monkeypatch.setattr(daemon.dispatch, "poll", lambda consumer: None)
    monkeypatch.setattr(run_consumer.RunConsumer, "poll", lambda self: polls.append(self.hive) or 0)
    registry.update(redis, "machine", ["name=Worker"])
    daemon.beat(redis, "machine")
    assert polls == ["machine"] * polled
