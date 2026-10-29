import pytest

from scripts.events.redis_state import RedisStateStore
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
    grants = [f"{ROOT}:scheduler:*", f"{ROOT}:swarm:state:scheduler:*"]
    admin.acl_setuser("controller", enabled=True, nopass=True, categories=["+@all"], keys=grants)
    controller = fakeredis.FakeRedis(server=server, decode_responses=True, username="controller")
    controller.auth("", username="controller")
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "distributed")
    monkeypatch.delenv("AGENTIHOOKS_HIVE_ID", raising=False)
    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    state = RedisStateStore(controller, "scheduler")
    with process_timer.Timer(0, state=state) as timer:
        assert Scheduler(state).run_due(now=timer.entry.record["due"]) == 1
        timer.wait(timeout=1)
        assert timer.entry.state == "fired"
    Scheduler(state).run_due()
    assert Scheduler(state).document()["entries"] == {}
