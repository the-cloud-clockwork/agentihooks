import pytest

from scripts.scheduler.core import Scheduler

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("deployment", ["cloud", "distributed"])
def test_controller_scheduler_keys_use_the_swarm_namespace(monkeypatch, deployment):
    from scripts.swarm.keyspace import ROOT

    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", deployment)
    monkeypatch.delenv("AGENTIHOOKS_HIVE_ID", raising=False)
    scheduler = Scheduler(None, machine="controller")
    for suffix in ("entries", "due", "pending", "retired", "dispatch-lock", "wake:one"):
        assert scheduler.key(suffix) == f"{ROOT}:scheduler:controller:{suffix}"


def test_controller_can_register_dispatch_and_retire_a_timer(monkeypatch):
    import fakeredis

    from scripts.scheduler import process_timer
    from scripts.swarm.keyspace import ROOT

    server = fakeredis.FakeServer()
    admin = fakeredis.FakeRedis(server=server, decode_responses=True)
    admin.acl_setuser("controller", enabled=True, nopass=True, categories=["+@all"], keys=[f"{ROOT}:scheduler:*"])
    controller = fakeredis.FakeRedis(server=server, decode_responses=True, username="controller")
    controller.auth("", username="controller")
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "distributed")
    monkeypatch.delenv("AGENTIHOOKS_HIVE_ID", raising=False)
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    monkeypatch.setattr(controller, "lock", admin.lock)
    with process_timer.Timer(0, redis=controller) as timer:
        assert Scheduler(controller).run_due(now=timer.entry.record["due"]) == 1
        assert controller.llen(timer.key) == 1
        assert controller.blpop(timer.key, timeout=0.2)[0] == timer.key
    Scheduler(controller).run_due()
    assert admin.keys("*") == []
