import json
from dataclasses import asdict

import pytest

from scripts.codex_session import protocol, stand_down
from scripts.codex_session.binding import CONNECTED, EXITED, PUBLISHING, STOOD_DOWN, BindingStore, TransportRecord
from scripts.inbox import codex
from scripts.inbox.store import InboxStore
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.terminate_agent import Process, Session, main
from tests.codex_session.fakes import Clock
from tests.inbox.test_codex_pump import AppServer

pytestmark = pytest.mark.unit

SLUG = "fixture"
OLD, NEW = "ci@abc-0815", "ci@abc-0816"
SERVER_PID = 4_190_001


class Rig:
    def __init__(self):
        import fakeredis

        server = fakeredis.FakeServer()
        self.inbox = InboxStore(fakeredis.FakeRedis(server=server, decode_responses=True))
        self.swarm = RedisStore(fakeredis.FakeRedis(server=server, decode_responses=True))
        self.swarm.create(SwarmConfig(SLUG, "agentihooks", 2, 0))
        self.bindings = BindingStore(self.swarm)
        self.clock = Clock()
        self.servers = {}
        self.connects = []
        self.timeouts = []
        self.add(OLD, "exe-old", "thread-old", 1)
        self.add(NEW, "exe-new", "thread-new", 2)

    def add(self, name, execution, thread, slot):
        seat = f"ci-{slot}@{SLUG}"
        endpoint = f"unix:///run/cx/{execution}/s"
        record = TransportRecord(execution, 1, seat, name, "t1", endpoint, server_pid=SERVER_PID + slot)
        self.bindings.reserve(SLUG, record)
        self.bindings.put(SLUG, TransportRecord(**{**asdict(record), "thread_id": thread, "state": CONNECTED}))
        agent = AgentRecord(name, "ci", "t1", seat=seat, execution_id=execution, generation=1)
        self.swarm.redis.hset(self.swarm.key(SLUG, "executions"), execution, json.dumps(asdict(agent)))
        self.servers[endpoint.removeprefix("unix://")] = AppServer(self.clock)

    def server(self, execution):
        return self.servers[f"/run/cx/{execution}/s"]

    def connect(self, path, timeout):
        self.connects.append(path)
        self.timeouts.append(timeout)
        return protocol.Connection(self.servers[path], 5, clock=self.clock), {}

    def pump(self, name, execution, thread):
        transport = codex.Transport(self.bindings, SLUG, execution)
        connection = protocol.Connection(self.server(execution), 5, clock=self.clock)
        binding = codex.Binding(connection, thread, name, f"host:{execution}", transport)
        timing = codex.Timing(clock=self.clock, now_ms=lambda: int(self.clock.now * 1000), reconcile_s=30)
        return codex.Pump(self.inbox, binding, timing, log=lambda line: None)

    def record(self, execution):
        return self.bindings.get(SLUG, execution)

    def steps(self, pump, count):
        left = iter(range(count))
        return pump.run(lambda: next(left, None) is None)

    def execute(self, plan, **kwargs):
        return stand_down.execute(
            self.bindings, plan, connect=self.connect, clock=self.clock, sleep=self.clock.sleep, **kwargs
        )


def sharing(session_id, name, pid=SERVER_PID + 1, argv=("codex", "app-server")):
    return Session(session_id, "codex", name, Process(pid, 1, pid, pid, 9, "S", "codex", argv), "/repo", "alive")


@pytest.fixture
def rig():
    return Rig()


@pytest.fixture(autouse=True)
def no_signals(monkeypatch):
    def refuse(*args):
        raise AssertionError("a stand down sent a signal")

    monkeypatch.setattr("os.kill", refuse)
    monkeypatch.setattr("os.killpg", refuse)


def busy(rig, execution):
    server = rig.server(execution)
    server.begin_elsewhere()
    server.frames.clear()
    return server


def test_a_stand_down_interrupts_the_named_conversation_and_leaves_the_other_running(rig):
    old, new = busy(rig, "exe-old"), busy(rig, "exe-new")
    plan = stand_down.plan(rig.bindings, SLUG, OLD, [])
    result = rig.execute(plan)
    assert result == {"stood_down": OLD, "thread": "thread-old", "interrupted": "turn-1"}
    assert old.turns[-1]["status"] == "interrupted"
    assert [m["params"] for m in old.sent if m["method"] == "turn/interrupt"] == [
        {"threadId": "thread-old", "turnId": "turn-1"}
    ]
    assert new.turns[-1]["status"] == codex.IN_PROGRESS
    assert new.methods() == []
    assert rig.connects == ["/run/cx/exe-old/s"]
    assert rig.timeouts == [stand_down.CONNECT_S]
    assert [m["params"]["threadId"] for m in old.sent if m["method"] == "thread/read"] == ["thread-old"] * 2
    assert rig.record("exe-old").state == STOOD_DOWN
    assert rig.record("exe-new").state == CONNECTED


def test_a_stood_down_conversation_takes_no_new_turn_from_its_inbox(rig):
    rig.execute(stand_down.plan(rig.bindings, SLUG, OLD, []))
    item = rig.inbox.send("master@abc", OLD, "keep pushing")
    assert rig.steps(rig.pump(OLD, "exe-old", "thread-old"), 3) == codex.DEGRADED
    assert "turn/start" not in rig.server("exe-old").methods()
    assert rig.inbox.get(item.id).state == "pending"
    assert rig.record("exe-old").state == STOOD_DOWN


def test_the_other_conversation_keeps_taking_turns_after_a_stand_down(rig):
    rig.execute(stand_down.plan(rig.bindings, SLUG, OLD, []))
    item = rig.inbox.send("master@abc", NEW, "carry on")
    assert rig.steps(rig.pump(NEW, "exe-new", "thread-new"), 1) == codex.STOPPED
    assert rig.server("exe-new").methods()[-1] == "turn/start"
    assert rig.inbox.get(item.id).state == "delivered"


def test_an_idle_conversation_is_fenced_without_an_interrupt(rig):
    result = rig.execute(stand_down.plan(rig.bindings, SLUG, "thread-old", []))
    assert result["interrupted"] == ""
    assert rig.server("exe-old").methods() == ["thread/read"]
    assert rig.record("exe-old").state == STOOD_DOWN


def test_the_interrupt_waits_for_an_inbox_publication_in_flight(rig):
    busy(rig, "exe-old")
    key = rig.swarm.key(SLUG, PUBLISHING)
    rig.swarm.redis.hset(key, f"ci-1@{SLUG}", "exe-old")
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        rig.clock.sleep(seconds)
        rig.swarm.redis.hdel(key, f"ci-1@{SLUG}")

    plan = stand_down.plan(rig.bindings, SLUG, OLD, [])
    result = stand_down.execute(rig.bindings, plan, connect=rig.connect, clock=rig.clock, sleep=sleep)
    assert sleeps == [stand_down.POLL_S]
    assert result["interrupted"] == "turn-1"


def test_a_publication_that_never_ends_stops_the_stand_down_before_the_interrupt(rig):
    busy(rig, "exe-old")
    rig.swarm.redis.hset(rig.swarm.key(SLUG, PUBLISHING), f"ci-1@{SLUG}", "exe-old")
    plan = stand_down.plan(rig.bindings, SLUG, OLD, [])
    with pytest.raises(RuntimeError, match="inbox publication .* still in flight"):
        rig.execute(plan, wait_s=1.0)
    assert rig.connects == []
    assert rig.record("exe-old").state == STOOD_DOWN
    assert rig.clock.now == pytest.approx(1.0)


def test_a_turn_still_running_after_the_interrupt_is_reported(rig):
    server = busy(rig, "exe-old")
    server.turn_interrupt = lambda params: {}
    with pytest.raises(RuntimeError, match="turn turn-1 still runs"):
        rig.execute(stand_down.plan(rig.bindings, SLUG, OLD, []), wait_s=1.0)


def test_the_dry_run_names_what_stops_and_what_keeps_running(rig):
    busy(rig, "exe-old")
    rig.bindings.put(SLUG, TransportRecord(**{**asdict(rig.record("exe-old")), "active_turn_id": "turn-1"}))
    others = [sharing("thread-old", OLD), sharing("sub-1", OLD), sharing("thread-new", NEW, pid=SERVER_PID + 2)]
    plan = stand_down.plan(rig.bindings, SLUG, OLD, others)
    assert plan.stops == (
        "conversation thread-old of ci@abc-0815 (execution exe-old generation 1): "
        "its active turn (last recorded: turn-1) is interrupted and it takes no new turns",
    )
    assert plan.keeps == (
        f"app-server pid {SERVER_PID + 1}: not signalled",
        "conversation sub-1 of ci@abc-0815: not interrupted",
    )
    assert rig.server("exe-old").sent == []
    assert rig.record("exe-old").state == CONNECTED


def test_the_dry_run_says_none_when_no_turn_was_recorded(rig):
    plan = stand_down.plan(rig.bindings, SLUG, OLD, [])
    assert "its active turn (last recorded: none) is interrupted" in plan.stops[0]


@pytest.mark.parametrize("selector", ["ci@abc-0700", "2448581"])
def test_a_conversation_without_a_managed_endpoint_is_refused_with_the_process_alternative(rig, selector):
    legacy = ("codex", "--no-daemon")
    others = [sharing(f"uuid-{n}", "ci@abc-0700", pid=2448581, argv=legacy) for n in (1, 2)]
    others.append(sharing("uuid-9", "ci@abc-0701", pid=2448999, argv=legacy))
    with pytest.raises(stand_down.Refused) as refused:
        stand_down.plan(rig.bindings, SLUG, selector, others)
    assert refused.value.reason == (
        f"{selector} has no managed Codex conversation in swarm fixture, "
        "so its conversations cannot be stopped one at a time"
    )
    assert refused.value.alternative == (
        "process 2448581 hosts conversations uuid-1, uuid-2; "
        "agentihooks terminate-agent 2448581 --force-shared --dry-run names its process group, "
        "and the same command without --dry-run ends every conversation in that process, "
        "so run it only when all of them belong to this agent"
    )


def test_an_unknown_name_is_refused_with_the_listing_alternative(rig):
    with pytest.raises(stand_down.Refused) as refused:
        stand_down.plan(rig.bindings, SLUG, "ci@abc-0999", [])
    assert refused.value.alternative == "agentihooks terminate-agent --list --type codex shows the live conversations"


def test_an_ended_conversation_is_refused_as_nothing_to_stand_down(rig):
    rig.bindings.put(SLUG, TransportRecord(**{**asdict(rig.record("exe-old")), "state": EXITED}))
    with pytest.raises(stand_down.Refused) as refused:
        stand_down.plan(rig.bindings, SLUG, OLD, [])
    assert (refused.value.reason, refused.value.alternative) == (
        "conversation thread-old of ci@abc-0815 already ended (exited)",
        "nothing needs standing down",
    )


def test_two_live_matches_are_refused_until_one_thread_is_named(rig):
    rig.add(OLD, "exe-again", "thread-again", 3)
    with pytest.raises(stand_down.Refused) as refused:
        stand_down.plan(rig.bindings, SLUG, OLD, [])
    assert refused.value.reason == "ci@abc-0815 names 2 live conversations"
    assert refused.value.alternative == "name one thread: thread-again, thread-old"


def test_the_command_prints_the_dry_run_and_sends_nothing(rig, monkeypatch, capsys):
    monkeypatch.setattr("scripts.terminate_agent.sessions", lambda: [])
    monkeypatch.setattr("scripts.swarm.store.connect", lambda: rig.swarm)
    assert main([OLD, "--stand-down", "--swarm", SLUG, "--dry-run"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "action=stand-down-dry-run"
    assert out[1].startswith("stops: conversation thread-old of ci@abc-0815")
    assert out[2] == f"keeps: app-server pid {SERVER_PID + 1}: not signalled"
    assert out[-1] == "result=validated signal=none"
    assert rig.record("exe-old").state == CONNECTED


def test_the_command_finds_the_swarm_from_the_agent_name(rig, monkeypatch, capsys):
    monkeypatch.setattr("scripts.terminate_agent.sessions", lambda: [])
    monkeypatch.setattr("scripts.swarm.store.connect", lambda: rig.swarm)
    monkeypatch.setattr(rig.swarm.names, "slug_of", lambda name: SLUG if name == OLD else "")
    monkeypatch.setattr(
        stand_down,
        "execute",
        lambda bindings, plan: {"stood_down": plan.record.agent_name, "store": bindings.store is rig.swarm},
    )
    assert main([OLD, "--stand-down"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "action=stand-down"
    assert out[-1] == "result=stood-down stood_down=ci@abc-0815 store=True"


def test_the_command_prints_a_refusal_with_its_safe_alternative(rig, monkeypatch, capsys):
    monkeypatch.setattr("scripts.terminate_agent.sessions", lambda: [])
    monkeypatch.setattr("scripts.swarm.store.connect", lambda: rig.swarm)
    assert main(["ci@abc-0999", "--stand-down", "--swarm", SLUG]) == 2
    assert capsys.readouterr().err.splitlines() == [
        "terminate-agent: refused: ci@abc-0999 has no managed Codex conversation in swarm fixture, "
        "so its conversations cannot be stopped one at a time",
        "terminate-agent: safe alternative: agentihooks terminate-agent --list --type codex shows the live conversations",
    ]


def test_the_command_without_a_swarm_names_the_flag(rig, monkeypatch, capsys):
    monkeypatch.setattr("scripts.terminate_agent.sessions", lambda: [])
    monkeypatch.setattr("scripts.swarm.store.connect", lambda: rig.swarm)
    assert main(["thread-old", "--stand-down"]) == 2
    assert capsys.readouterr().err.splitlines() == [
        "terminate-agent: refused: no swarm given and thread-old names no swarm agent",
        "terminate-agent: safe alternative: pass --swarm <slug>",
    ]


def test_a_failed_stand_down_exits_one_with_its_reason(rig, monkeypatch, capsys):
    monkeypatch.setattr("scripts.terminate_agent.sessions", lambda: [])
    monkeypatch.setattr("scripts.swarm.store.connect", lambda: rig.swarm)

    def fail(bindings, plan):
        raise protocol.ProtocolError("cannot open the app-server socket: ConnectionRefusedError")

    monkeypatch.setattr(stand_down, "execute", fail)
    assert main([OLD, "--stand-down", "--swarm", SLUG]) == 1
    assert capsys.readouterr().err.splitlines() == [
        "terminate-agent: cannot open the app-server socket: ConnectionRefusedError",
        "terminate-agent: ci@abc-0815 stays fenced from new inbox turns; "
        "run the same stand down again to interrupt its turn",
    ]


def test_the_fence_keeps_what_the_pump_wrote_after_the_plan(rig):
    chosen = stand_down.plan(rig.bindings, SLUG, OLD, [])
    rig.bindings.put(SLUG, TransportRecord(**{**asdict(rig.record("exe-old")), "last_reconcile_at": 4242}))
    rig.execute(chosen)
    assert (rig.record("exe-old").state, rig.record("exe-old").last_reconcile_at) == (STOOD_DOWN, 4242)


@pytest.fixture
def command(rig, monkeypatch):
    monkeypatch.setattr("scripts.terminate_agent.sessions", lambda: [])
    monkeypatch.setattr("scripts.swarm.store.connect", lambda: rig.swarm)
    return monkeypatch


def test_a_redis_error_after_the_fence_exits_one_and_says_it_stays_fenced(rig, command, capsys):
    def fail(*args):
        from redis.exceptions import ConnectionError as RedisConnectionError

        raise RedisConnectionError("Connection refused")

    command.setattr(stand_down, "_settle", fail)
    assert main([OLD, "--stand-down", "--swarm", SLUG]) == 1
    assert capsys.readouterr().err.splitlines() == [
        "terminate-agent: Connection refused",
        "terminate-agent: ci@abc-0815 stays fenced from new inbox turns; "
        "run the same stand down again to interrupt its turn",
    ]
    assert rig.record("exe-old").state == STOOD_DOWN


def test_a_redis_error_in_the_fence_exits_one_and_says_it_is_not_fenced(rig, command, capsys):
    def fail(*args):
        from redis.exceptions import ConnectionError as RedisConnectionError

        raise RedisConnectionError("Connection refused")

    command.setattr(stand_down, "_fence", fail)
    assert main([OLD, "--stand-down", "--swarm", SLUG]) == 1
    assert capsys.readouterr().err.splitlines() == [
        "terminate-agent: Connection refused",
        "terminate-agent: ci@abc-0815 is not fenced; run the same stand down again",
    ]
    assert rig.record("exe-old").state == CONNECTED


def test_a_redis_error_before_the_plan_exits_one_and_says_nothing_is_fenced(rig, command, capsys):
    def fail(*args):
        from redis.exceptions import ConnectionError as RedisConnectionError

        raise RedisConnectionError("Connection refused")

    command.setattr(stand_down, "plan", fail)
    assert main([OLD, "--stand-down", "--swarm", SLUG]) == 1
    assert capsys.readouterr().err.splitlines() == [
        "terminate-agent: Connection refused",
        "terminate-agent: ci@abc-0815 is not fenced; run the same stand down again",
    ]


def test_an_unregistered_process_is_refused_as_hosting_unknown_conversations(rig, command, capsys):
    unregistered = [sharing("", "", pid=2448581, argv=("codex", "--no-daemon"))]
    command.setattr("scripts.terminate_agent.sessions", lambda: unregistered)
    assert main(["2448581", "--stand-down", "--swarm", SLUG]) == 2
    assert capsys.readouterr().err.splitlines()[1] == (
        "terminate-agent: safe alternative: process 2448581 hosts conversations that agentihooks has not registered, "
        "so which ones is unknown; "
        "agentihooks terminate-agent 2448581 --force-shared --dry-run names its process group, "
        "and the same command without --dry-run ends every conversation in that process, "
        "so run it only when all of them belong to this agent"
    )


def test_the_fence_leaves_a_record_whose_agent_exited_after_the_plan(rig, command, capsys):
    planned = stand_down.plan

    def plan_then_exit(*args):
        chosen = planned(*args)
        rig.bindings.put(SLUG, TransportRecord(**{**asdict(rig.record("exe-old")), "state": EXITED}))
        return chosen

    command.setattr(stand_down, "plan", plan_then_exit)
    assert main([OLD, "--stand-down", "--swarm", SLUG]) == 2
    assert capsys.readouterr().err.splitlines() == [
        "terminate-agent: refused: conversation thread-old of ci@abc-0815 already ended (exited)",
        "terminate-agent: safe alternative: nothing needs standing down",
    ]
    assert rig.record("exe-old").state == EXITED
    assert rig.connects == []


def test_the_command_refuses_a_claude_type(rig, command, capsys):
    assert main([OLD, "--stand-down", "--swarm", SLUG, "--type", "claude"]) == 2
    assert capsys.readouterr().err.splitlines() == [
        "terminate-agent: --stand-down stops Codex conversations only, not --type claude",
    ]
    assert rig.record("exe-old").state == CONNECTED


@pytest.mark.parametrize("kind", ["codex", "any"])
def test_the_command_never_offers_a_claude_process_as_the_alternative(rig, command, capsys, kind):
    process = Process(2448581, 1, 2448581, 2448581, 9, "S", "claude", ("claude",))
    claude = Session("uuid-c", "claude", "ci@abc-0700", process, "/repo", "alive")
    command.setattr("scripts.terminate_agent.sessions", lambda: [claude])
    assert main(["2448581", "--stand-down", "--swarm", SLUG, "--type", kind]) == 2
    assert capsys.readouterr().err.splitlines()[1] == (f"terminate-agent: safe alternative: {stand_down.LISTING}")


def test_the_command_prints_one_json_report_for_a_dry_run(rig, command, capsys):
    assert main([OLD, "--stand-down", "--swarm", SLUG, "--dry-run", "--json", "--type", "codex"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "action": "stand-down-dry-run",
        "stops": [
            "conversation thread-old of ci@abc-0815 (execution exe-old generation 1): "
            "its active turn (last recorded: none) is interrupted and it takes no new turns"
        ],
        "keeps": [f"app-server pid {SERVER_PID + 1}: not signalled"],
        "result": "validated",
        "signal": "none",
    }


def test_the_command_prints_one_json_report_for_a_stand_down(rig, command, capsys):
    command.setattr(stand_down, "execute", lambda bindings, plan: {"stood_down": OLD, "interrupted": ""})
    assert main([OLD, "--stand-down", "--swarm", SLUG, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["action"], report["result"], report["stood_down"], report["interrupted"]) == (
        "stand-down",
        "stood-down",
        OLD,
        "",
    )
