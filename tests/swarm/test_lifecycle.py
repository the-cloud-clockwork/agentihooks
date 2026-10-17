import pytest

from scripts.swarm import lifecycle
from scripts.swarm.store import APP_SERVER_TRANSPORT, AgentRecord

pytestmark = pytest.mark.unit

MANAGED = AgentRecord("sw-eng-1", "eng", "t1", harness="codex", transport=APP_SERVER_TRANSPORT, execution_id="exe-1")
LEGACY = AgentRecord("sw-eng-2", "eng", "t2", harness="codex", execution_id="exe-2")


class Runtime:
    def __init__(self, retired):
        self.retired, self.seen = retired, []

    def retire(self, agent):
        self.seen.append(agent)
        return self.retired

    def refusal(self, agent):
        return {"process": 7, "refusal": f"managed server identity of {agent.name} is unknown"}


@pytest.mark.parametrize(("retired", "error"), [(True, ""), (False, "managed server identity of sw-eng-1 is unknown")])
def test_a_managed_terminate_retires_the_recorded_server_and_never_runs_terminate_agent(monkeypatch, retired, error):
    runtime, ran = Runtime(retired), []
    monkeypatch.setattr("scripts.swarm.runtime.HerdrRuntime", lambda: runtime)
    assert lifecycle._apply(object(), "sw", MANAGED, {"control": "terminate"}, ran.append) == (error, {})
    assert runtime.seen == [MANAGED]
    assert ran == []


def test_an_unmanaged_terminate_still_runs_terminate_agent(monkeypatch):
    monkeypatch.setattr("scripts.swarm.runtime.HerdrRuntime", lambda: pytest.fail("runtime retire"))
    ran = []
    assert lifecycle._apply(object(), "sw", LEGACY, {"control": "terminate"}, lambda argv: ran.append(argv) or "") == (
        "",
        {},
    )
    assert ran == [
        ["terminate-agent", "sw-eng-2", "--type", "any", "--dry-run"],
        ["terminate-agent", "sw-eng-2", "--type", "any"],
    ]
