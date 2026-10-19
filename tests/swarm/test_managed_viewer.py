from dataclasses import replace

import pytest

from scripts.inbox.store import InboxStore
from scripts.inbox.wake import BY
from scripts.swarm import runtime
from scripts.swarm.store import LEGACY_TRANSPORT
from tests.swarm.test_managed_lifecycle import rig  # noqa: F401

pytestmark = pytest.mark.unit


def test_a_managed_nudge_lands_in_the_inbox_and_never_types_into_the_viewer(rig):  # noqa: F811
    assert rig.runner.nudge(rig.agent, "You sit idle on your task.") is None
    inbox = InboxStore(rig.store.redis)
    items = inbox.inbox(inbox.names.resolve(rig.agent.name))
    assert [(item.sender, item.text, item.fyi) for item in items] == [(BY, "You sit idle on your task.", True)]
    rig.runner.herdr.assert_not_called()


def test_a_failed_managed_nudge_reports_why(rig, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runtime, "connect", lambda: (_ for _ in ()).throw(OSError("redis down")))
    assert rig.runner.nudge(rig.agent, "You sit idle.") == "redis down"
    rig.runner.herdr.assert_not_called()


def test_a_legacy_codex_nudge_still_types_into_its_pane(rig):  # noqa: F811
    rig.runner.herdr.side_effect = None
    legacy = replace(rig.agent, transport=LEGACY_TRANSPORT)
    assert rig.runner.nudge(legacy, "You sit idle.") is None
    command = rig.runner.herdr.call_args.args[0]
    assert command[:3] == ["agent", "prompt", runtime.pane_target(legacy)]
    inbox = InboxStore(rig.store.redis)
    assert inbox.inbox(inbox.names.resolve(rig.agent.name)) == []


def test_a_managed_agent_never_resumes_into_a_new_pane():
    from types import SimpleNamespace

    from scripts.swarm.store import APP_SERVER_TRANSPORT, AgentRecord

    agent = AgentRecord(
        "sw-eng-1",
        "eng",
        "t1",
        harness="codex",
        profile="engineer",
        transport=APP_SERVER_TRANSPORT,
        execution_id="exe-1",
    )
    launched = []
    herdr = runtime.HerdrRuntime(herdr=lambda args: launched.append(args) or {})
    with pytest.raises(runtime.SpawnError) as refused:
        herdr.resume(SimpleNamespace(slug="sw"), agent, "Continue.")
    assert (str(refused.value), refused.value.status) == (
        "unsupported resume: a managed Codex execution continues through its supervised server",
        "unsupported",
    )
    assert launched == []
