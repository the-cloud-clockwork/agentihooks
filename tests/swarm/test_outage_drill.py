from collections import Counter
from dataclasses import replace

import pytest

pytestmark = pytest.mark.unit

MINUTE = 60_000
SENDERS = ("direct message", "swarm message", "operator chat", "operator comment", "operator answer", "tick notice")


class Drill:
    def __init__(self, server, store, ledger, runtime, monkeypatch):
        from scripts.inbox.store import InboxStore
        from scripts.swarm import cli

        self.server, self.store, self.ledger, self.runtime = server, store, ledger, runtime
        self.inbox, self.clock = InboxStore(store.redis), 1_000
        self.errors, self.accepted, self.confirmed = [], [], []
        monkeypatch.setattr(cli, "now_ms", lambda: self.clock)
        self.tick()
        store.update("sw", state="paused")
        self.agents = [a for a in store.agents("sw") if a.lane == "eng"]
        self.master = next(a for a in store.agents("sw") if a.lane == "master")
        for index, agent in enumerate(self.agents):
            store.put_agent("sw", replace(agent, conversation_id=f"conversation-{index}", profile="engineer"))
        self.claims = {a.task: store.claimant("sw", a.task) for a in self.agents}
        self.launches = list(runtime.spawned)

    def tick(self):
        from scripts.swarm import cli

        return self.attempt(lambda: cli.run_tick(self.store, "sw", self.ledger, self.runtime, self.runtime))

    def attempt(self, call):
        from redis.exceptions import RedisError

        from scripts.swarm.store import SwarmError

        try:
            return call()
        except (RedisError, SwarmError) as exc:
            self.errors.append(str(exc))
            return None

    def stop(self, kind):
        self.clock = MINUTE
        for agent in self.agents:
            self.runtime.statuses[agent.name] = "idle"
        if kind == "redis":
            self.server.connected = False
        elif kind == "network":
            self.ledger.online = False
        else:
            self.runtime.errors[self.agents[0].name] = "API Error: 503 provider unavailable"
        self.tick()

    def send(self, monkeypatch):
        from scripts.inbox import cli
        from scripts.swarm import cli as swarm_cli
        from scripts.swarm_ledger import ledger_server

        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", self.master.name)
        self.accepted.append(cli.main(["send", self.agents[0].name, "drill direct message"]))
        self.accepted.append(swarm_cli.main(["sw", "say", "drill swarm message", "--to", self.agents[0].name]))
        events = (
            ("operator", "chat added", "chat", f"@{self.agents[0].name} drill operator chat"),
            ("operator", "comment added", f"tasks/{self.agents[0].task}", "drill operator comment"),
            ("operator", "answer added", f"tasks/{self.agents[0].task}", "drill operator answer"),
            (self.agents[0].name, "added", "questions/drill", "drill tick notice"),
        )
        for by, kind, target, text in events:
            self.ledger.write(by, kind, target, text, self.clock)
            ledger_server.relay_to_inbox("sw", self.ledger.document("sw"))
        self.tick()

    def recover(self):
        from scripts.swarm_ledger import ledger_server

        self.server.connected, self.ledger.online, self.runtime.healthy = True, True, True
        ledger_server.relay_to_inbox("sw", self.ledger.state("sw"))
        ledger_server.replay_unrelayed()
        for step in range(3):
            self.clock = (10 + step) * MINUTE
            self.tick()
        ledger_server.relay_to_inbox("sw", self.ledger.state("sw"))
        ledger_server.replay_unrelayed()

    def items(self):
        return {
            item.id: item
            for agent in [self.master, *self.agents]
            for address in (agent.name, agent.seat)
            for item in self.inbox.inbox(address)
            if "drill" in item.text
        }

    def confirm(self):
        import anyio

        from scripts.inbox import channel

        rig = self

        class Receiver:
            def __init__(self, agent):
                self.agent = agent

            async def send(self, message):
                params = message.message.root.params
                item_id = params["meta"]["item_id"]
                assert rig.inbox.get(item_id).state == "delivered"
                rig.inbox.read(item_id, self.agent.name)
                rig.inbox.close(item_id, self.agent.name, "done", "drill receiver confirmed")
                if item_id in rig.items():
                    rig.confirmed.append(item_id)

        async def deliver():
            for agent in [rig.master, *rig.agents]:
                await channel._deliver(rig.inbox, agent.name, Receiver(agent), channel.Pushed())

        anyio.run(deliver)


def fake_ledger():
    from scripts.swarm.ledger_client import LedgerClient
    from scripts.swarm.store import SwarmError
    from tests.swarm.test_tick import FakeLedger

    class Ledger(FakeLedger, LedgerClient):
        time_left = None

        def metadata(self, slug):
            return self.state(slug)["_meta"]

        def _call(self, slug, ops=None):
            raise AssertionError("unexpected ledger transport")

        def __init__(self):
            super().__init__([{"id": f"t{index}", "lane": "eng"} for index in range(3)])
            self.online, self.log = True, []

        def state(self, slug):
            if not self.online:
                raise SwarmError("ledger network unavailable")
            return self.document(slug)

        def document(self, slug):
            return super().state(slug)

        def write(self, by, kind, target, text, at):
            rev = len(self.log) + 1
            self.log.append(
                {"rev": rev, "at": at, "by": by, "kind": kind, "target": target, "id": f"drill-{rev}", "text": text}
            )

    return Ledger()


def fake_runtime():
    from scripts.swarm.pane import PaneObservation
    from scripts.swarm.runtime import Placed
    from tests.swarm.test_tick import FakeRuntime

    class Runtime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.errors, self.resumed, self.continued, self.healthy = {}, [], [], False

        def observe(self, agent):
            return PaneObservation(self.status(agent), provider_error=self.errors.get(agent.name, ""))

        def nudge(self, agent, text):
            self.continued.append((agent.name, text))
            result = super().nudge(agent, text)
            if not result:
                self.statuses[agent.name] = "working"
                self.errors.pop(agent.name, None)
            return result

        def resume(self, config, agent, text):
            self.resumed.append((agent.name, agent.task, agent.conversation_id, text))
            self.live.add(agent.name)
            self.statuses[agent.name] = "working"
            return Placed("drill:recovered", agent.harness, profile=agent.profile)

        def agent_status(self, agent):
            return self.status(agent)

        def typed_input(self, agent):
            return ""

        def prompt(self, agent, text):
            return "provider unavailable" if not self.healthy else None

    return Runtime()


@pytest.fixture
def drill(tmp_path, monkeypatch):
    import fakeredis

    from scripts.inbox import cli as inbox_cli
    from scripts.inbox import outbox
    from scripts.inbox.store import InboxStore, Unreachable
    from scripts.swarm import cli as swarm_cli
    from scripts.swarm import outage, snapshot
    from scripts.swarm.store import RedisStore, SwarmConfig
    from scripts.swarm_ledger import ledger_link, ledger_server, storage

    server = fakeredis.FakeServer()
    redis = fakeredis.FakeRedis(server=server, decode_responses=True)
    store = RedisStore(redis)
    store.create(SwarmConfig("sw", str(tmp_path), max_eng=3, max_ci=0))
    ledger, runtime = fake_ledger(), fake_runtime()

    def connect():
        if not server.connected:
            raise Unreachable("drill Redis unavailable")
        return InboxStore(redis)

    def relay_connect():
        if not ledger.online:
            raise Unreachable("drill relay network unavailable")
        return connect()

    monkeypatch.setattr("scripts.inbox.store.connect", relay_connect)
    monkeypatch.setattr(inbox_cli, "connect", connect)
    monkeypatch.setattr(swarm_cli, "connect", lambda: store)
    monkeypatch.setattr(outbox, "home", lambda: tmp_path / "outbox")
    monkeypatch.setattr(outage, "HOME", tmp_path / "outages")
    monkeypatch.setattr(ledger_server, "stored", storage.build(tmp_path / storage.DATABASE))
    monkeypatch.setattr(ledger_server.repository, "get_document", ledger.state)
    monkeypatch.setattr(ledger_link, "serving", lambda: ledger.online)
    monkeypatch.setattr(snapshot, "worktrees", lambda repo, names: {name: str(tmp_path) for name in names})
    monkeypatch.setenv("AGENTIHOOKS_SWARM_SNAPSHOT_MINUTES", "0")
    monkeypatch.setenv("AGENTIHOOKS_INBOX_QUIET_S", "0")
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_SWARM_TASK", raising=False)
    return Drill(server, store, ledger, runtime, monkeypatch)


def probe_recovery(drill, monkeypatch):
    from scripts.swarm import provider_probe

    calls = []

    def check(target, directory):
        calls.append((target, drill.runtime.healthy))
        return provider_probe.Result(drill.runtime.healthy, "drill account request")

    monkeypatch.setattr(provider_probe, "check", check)
    return calls


def test_channel_delivery_after_redis_recovery_is_confirmed_once(drill, monkeypatch):
    from scripts.inbox import cli

    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", drill.master.name)
    drill.stop("redis")
    assert cli.main(["send", drill.agents[0].name, "drill direct message"]) == 0
    drill.recover()
    found = drill.items()
    assert len(found) == 1
    [item] = found.values()
    assert item.address in (drill.agents[0].name, drill.agents[0].seat)
    drill.confirm()
    drill.clock += MINUTE
    drill.tick()
    drill.confirm()
    assert drill.confirmed == [item.id]
    assert drill.inbox.get(item.id).state == "done"
    states = [entry["state"] for entry in drill.inbox.history(item.id)]
    assert states.count("delivered") == 1 and states.count("read") == 1


@pytest.mark.parametrize("kind", ("redis", "network", "provider"))
def test_every_sender_survives_an_outage_and_is_delivered_and_confirmed_once(drill, monkeypatch, kind):
    if kind == "provider":
        probe_recovery(drill, monkeypatch)
    drill.stop(kind)
    drill.send(monkeypatch)
    drill.recover()
    found = drill.items()
    assert Counter(
        sender for item in found.values() for sender in SENDERS if f"drill {sender}" in item.text
    ) == Counter(SENDERS), drill.errors
    assert drill.accepted == [0, 0]
    for item in found.values():
        receiver = drill.master if "drill tick notice" in item.text else drill.agents[0]
        assert item.address in (receiver.name, receiver.seat)
    drill.confirm()
    assert Counter(drill.confirmed) == Counter(found.keys())
    for item_id in found:
        assert drill.inbox.get(item_id).state == "done"
        states = [entry["state"] for entry in drill.inbox.history(item_id)]
        assert states.count("delivered") == 1 and states.count("read") == 1
    drill.clock += MINUTE
    drill.tick()
    assert set(drill.items()) == set(found)
    drill.confirm()
    assert Counter(drill.confirmed) == Counter(found.keys())


@pytest.mark.parametrize("kind", ("redis", "network", "provider"))
def test_outage_stalled_owners_resume_within_three_ticks_without_a_new_claim(drill, monkeypatch, kind):
    calls = probe_recovery(drill, monkeypatch) if kind == "provider" else []
    drill.stop(kind)
    drill.runtime.live.remove(drill.agents[2].name)
    for step in range(3):
        drill.clock = (5 + step) * MINUTE
        drill.tick()
    drill.server.connected = True
    assert {a.task: drill.store.claimant("sw", a.task) for a in drill.agents} == drill.claims
    assert drill.runtime.spawned == drill.launches
    drill.recover()
    assert all(drill.runtime.statuses[a.name] == "working" for a in drill.agents), drill.errors
    assert {a.task: drill.store.claimant("sw", a.task) for a in drill.agents} == drill.claims
    assert drill.runtime.spawned == drill.launches
    assert [(name, task, conversation) for name, task, conversation, _ in drill.runtime.resumed] == [
        (drill.agents[2].name, drill.agents[2].task, "conversation-2")
    ]
    assert Counter(drill.runtime.nudged) == Counter(a.name for a in drill.agents[:2])
    owners = {a.name: a for a in drill.agents}
    for name, text in drill.runtime.continued:
        assert "Service is back" in text
        assert f"continue your own task {owners[name].task}" in text
        assert "steering.md" in text and "progress.md" in text and "proof.md" in text
        assert "agentihooks msg inbox" in text and "Keep your existing claim" in text
    if kind == "provider":
        assert any(healthy for _, healthy in calls)
