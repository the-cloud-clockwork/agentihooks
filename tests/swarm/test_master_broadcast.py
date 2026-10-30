import json

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm.store import RedisStore
from tests.swarm.test_cli import env as env
from tests.swarm.test_cli import run
from tests.swarm.test_tick import FakeLedger, FakeRuntime


def test_send_message_reaches_every_live_agent_through_the_inbox_and_never_chat(
    env: tuple[RedisStore, FakeLedger, FakeRuntime],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, ledger, _ = env
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    ledger.said.clear()
    capsys.readouterr()
    assert run("sw", "send-message", "pause new work for a moment") == 0
    assert ledger.said == []
    live = sorted(a.name for a in store.agents("sw"))
    inbox = InboxStore(store.redis)
    for agent in store.agents("sw"):
        address = agent.seat if agent.lane == "master" else agent.name
        [item] = [i for i in inbox.inbox(address) if not i.fyi]
        assert (item.sender, item.text) == ("operator", "pause new work for a moment")
    assert sorted(json.loads(capsys.readouterr().out)["sent"]) == live and len(live) == 3
