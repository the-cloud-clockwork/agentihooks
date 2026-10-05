from dataclasses import replace

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import cli
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


@pytest.fixture
def controls(monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("demo", "/repo", state="running", max_eng=0, max_ci=0))
    master = AgentRecord("demo-master-1", "master", "master", seat="master@demo")
    store.put_agent("demo", master)
    store.seats.occupy(master.seat, master.name, 1)
    ledger = FakeLedger([])
    ledger.said = []
    ledger.say = lambda slug, text, by=None: ledger.said.append((text, by))
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    monkeypatch.setattr(cli, "run_tick", lambda *args: [])
    monkeypatch.setattr(cli, "HerdrRuntime", FakeRuntime)
    monkeypatch.setattr(cli.timer, "ensure", lambda *args: True)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "operator")
    monkeypatch.delenv("AGENTIHOOKS_CONTROL_SOURCE", raising=False)
    return store, ledger, master


def test_external_pause_tells_the_master_once_with_the_resulting_state(controls):
    store, ledger, master = controls
    assert cli.main(["demo", "pause"]) == 0
    items = InboxStore(store.redis).mailbox(master.name)
    assert len(items) == 1
    assert items[0].sender == "operator"
    assert items[0].fyi
    assert items[0].text == "The operator paused the swarm from the command line. The swarm is paused."
    assert ledger.said == [(items[0].text, "swarm")]


@pytest.mark.parametrize("explicit", [False, True])
def test_the_masters_own_pause_sends_it_nothing(controls, monkeypatch, explicit):
    store, ledger, master = controls
    if not explicit:
        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", master.name)
    argv = ["demo", "--as", master.name, "pause"] if explicit else ["demo", "pause"]
    assert cli.main(argv) == 0
    assert store.config("demo").state == "paused"
    assert InboxStore(store.redis).mailbox(master.name) == []
    assert ledger.said == []


def test_a_master_without_a_seat_receives_the_notification_by_name(controls):
    store, ledger, master = controls
    store.put_agent("demo", replace(master, seat=""))
    assert cli.main(["demo", "pause"]) == 0
    items = InboxStore(store.redis).inbox(master.name)
    assert len(items) == 1
    assert items[0].fyi
    assert ledger.said == [(items[0].text, "swarm")]


def test_failed_cli_settings_send_nothing(controls):
    store, ledger, master = controls
    assert cli.main(["demo", "set", "max-eng-agents=bad"]) == 1
    assert InboxStore(store.redis).mailbox(master.name) == []
    assert ledger.said == []


def test_another_named_caller_is_identified_in_the_notice(controls):
    store, ledger, master = controls
    assert cli.main(["demo", "--as", "worker-2", "pause"]) == 0
    items = InboxStore(store.redis).mailbox(master.name)
    assert len(items) == 1
    assert items[0].sender == "worker-2"
    assert items[0].text.startswith("worker 2 paused the swarm from the command line.")
    assert ledger.said == [(items[0].text, "swarm")]
