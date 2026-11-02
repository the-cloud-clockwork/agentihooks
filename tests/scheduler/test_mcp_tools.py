import json

import pytest

from hooks.mcp import scheduler as scheduler_tools
from scripts.events.redis_state import RedisStateStore
from scripts.scheduler import commands

pytestmark = pytest.mark.unit

NOW = 1000.0
SID = "sid-caller"

TOOLS = {
    "timer_add",
    "timer_list",
    "timer_cancel",
    "stopwatch_start",
    "stopwatch_stop",
    "stopwatch_show",
    "cron_add",
    "cron_list",
    "cron_remove",
}


class Collector:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def keep(fn):
            self.tools[fn.__name__] = fn
            return fn

        return keep


@pytest.fixture
def scheduler(monkeypatch):
    import fakeredis

    from scripts.inbox import cli as inbox_cli
    from scripts.scheduler.core import Scheduler
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    found = Scheduler(RedisStateStore(fakeredis.FakeRedis(decode_responses=True), "scheduler"), machine="proof")
    store = RedisStore(found.state.redis)
    store.create(SwarmConfig("sw", "/repo", 2, 1))
    for name, lane in (("sw-eng-1", "eng"), ("sw-eng-2", "eng")):
        store.put_agent("sw", AgentRecord(name, lane, "t"))
    monkeypatch.setattr(commands, "connect", lambda: found)
    monkeypatch.setattr(commands.time, "time", lambda: NOW)
    monkeypatch.setattr(inbox_cli, "registered_name", lambda: "")
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    return found


@pytest.fixture
def tools(scheduler):
    collector = Collector()
    scheduler_tools.register(collector)
    return {name: lambda *a, fn=fn, **k: json.loads(fn(*a, **k)) for name, fn in collector.tools.items()}


@pytest.fixture
def logged(monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler_tools, "log", lambda message, data: calls.append((message, data)))
    return calls


def test_the_scheduler_category_registers_every_tool(tools):
    assert set(tools) == TOOLS


def test_timer_add_reaches_the_fleet_as_the_calling_session(tools, scheduler):
    answer = tools["timer_add"]("hello", delay="90")
    entry = answer["entry"]
    assert answer == {"success": True, "entry": entry}
    assert entry == {
        "id": entry["id"],
        "kind": "timer",
        "state": "active",
        "owner": SID,
        "payload": "hello",
        "due": NOW + 90,
        "targets": [{"kind": "broadcast", "channel": None}],
    }
    assert scheduler.get(entry["id"]).state == "active"


def test_timer_add_at_a_time_for_a_named_target(tools):
    entry = tools["timer_add"](
        "wake", at="2026-10-18T09:30:00+00:00", target="seat", name="master@sw", session_id="sid-given"
    )["entry"]
    assert (entry["due"], entry["owner"]) == (1792315800.0, "sid-given")
    assert entry["targets"] == [{"kind": "inbox", "address": "master@sw"}]


@pytest.mark.parametrize("when", [{}, {"delay": "5", "at": "2026-10-18T09:30:00+00:00"}])
def test_timer_add_needs_delay_or_at(tools, logged, when):
    assert tools["timer_add"]("x", **when) == {"success": False, "error": "give delay or at"}
    assert logged == [("MCP timer_add failed", {"error": "give delay or at"})]


def test_a_tool_without_a_caller_is_refused(tools, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    message = "this session has no identity: set AGENTIHOOKS_AGENT_NAME or run inside a Claude Code session"
    assert tools["timer_add"]("x", delay="5") == {"success": False, "error": message}
    assert tools["stopwatch_start"]("x", "5") == {"success": False, "error": message}
    assert tools["cron_add"]("x", "* * * * *") == {"success": False, "error": message}


def test_a_shared_server_needs_the_caller_named(tools, monkeypatch):
    from hooks.mcp import _session

    monkeypatch.setattr(_session, "_ENV_FALLBACK_ALLOWED", False)
    message = "pass agent_name or session_id: this server cannot tell which session is calling"
    assert tools["timer_add"]("x", delay="5") == {"success": False, "error": message}
    assert tools["stopwatch_start"]("x", "5") == {"success": False, "error": message}
    assert tools["cron_add"]("x", "* * * * *") == {"success": False, "error": message}
    assert tools["timer_add"]("x", delay="5", session_id="sid-given")["entry"]["owner"] == "sid-given"


@pytest.mark.parametrize("shared", [False, True])
def test_a_named_agent_owns_its_schedules_and_a_swarm_fire_skips_it(tools, scheduler, monkeypatch, shared):
    from hooks.mcp import _session
    from scripts.inbox.store import InboxStore

    monkeypatch.setattr(_session, "_ENV_FALLBACK_ALLOWED", not shared)
    named = {"target": "swarm", "name": "sw", "session_id": SID, "agent_name": "sw-eng-1"}
    assert tools["stopwatch_start"]("watch", "1m", **named)["entry"]["owner"] == "sw-eng-1"
    assert tools["cron_add"]("cron", "0 9 * * *", **named)["entry"]["owner"] == "sw-eng-1"
    assert tools["timer_add"]("stand up", delay="0", **named)["entry"]["owner"] == "sw-eng-1"
    assert scheduler.run_due(now=NOW) == 1
    inbox = InboxStore(scheduler.state.redis)
    assert inbox.inbox("sw-eng-1") == []
    assert [(item.sender, item.text) for item in inbox.inbox("sw-eng-2")] == [("sw-eng-1", "stand up")]


@pytest.mark.parametrize("session_id", ["", SID])
def test_an_agent_owns_its_schedule_by_name_and_a_swarm_fire_skips_it(tools, scheduler, monkeypatch, session_id):
    from scripts.inbox.store import InboxStore

    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "sw-eng-1")
    entry = tools["timer_add"]("stand up", delay="0", target="swarm", name="sw", session_id=session_id)["entry"]
    assert entry["owner"] == "sw-eng-1"
    assert scheduler.run_due(now=NOW) == 1
    inbox = InboxStore(scheduler.state.redis)
    assert inbox.inbox("sw-eng-1") == []
    assert [(item.sender, item.text) for item in inbox.inbox("sw-eng-2")] == [("sw-eng-1", "stand up")]


def test_timer_list_and_cancel(tools):
    late = tools["timer_add"]("late", delay="2m", target="swarm", name="sw")["entry"]
    soon = tools["timer_add"]("soon", delay="1m", target="session", name="sid-2")["entry"]
    listed = tools["timer_list"]()
    assert (listed["success"], listed["count"]) == (True, 2)
    assert [entry["id"] for entry in listed["entries"]] == [soon["id"], late["id"]]
    assert tools["timer_cancel"](soon["id"]) == {
        "success": True,
        "entry": {**soon, "state": "cancelled"},
    }
    assert tools["timer_list"]()["entries"] == [late]
    assert tools["timer_cancel"](soon["id"]) == {
        "success": False,
        "error": f"timer {soon['id']} is already cancelled",
    }


def test_stopwatch_start_show_and_stop(tools, monkeypatch):
    answer = tools["stopwatch_start"]("over time", "1m", target="seat", name="master@sw")
    entry = answer["entry"]
    assert entry == {
        "id": entry["id"],
        "kind": "stopwatch",
        "state": "active",
        "owner": SID,
        "payload": "over time",
        "due": NOW + 60,
        "targets": [{"kind": "inbox", "address": "master@sw"}],
        "limit": 60.0,
    }
    monkeypatch.setattr(commands.time, "time", lambda: NOW + 20)
    assert tools["stopwatch_show"](entry["id"]) == {"success": True, "entry": {**entry, "elapsed": 20}}
    assert tools["stopwatch_stop"](entry["id"]) == {
        "success": True,
        "entry": {**entry, "state": "stopped", "elapsed": 20},
    }
    assert tools["stopwatch_show"]("nope") == {"success": False, "error": "no stopwatch nope"}
    assert tools["stopwatch_stop"]("nope") == {"success": False, "error": "no stopwatch nope"}


def test_stopwatch_start_defaults_to_the_fleet(tools):
    assert tools["stopwatch_start"]("x", "5")["entry"]["targets"] == [{"kind": "broadcast", "channel": None}]
    assert tools["stopwatch_start"]("x", "later") == {
        "success": False,
        "error": "later is not a duration such as 90, 90s, 5m or 2h",
    }


def test_cron_add_list_and_remove(tools):
    entry = tools["cron_add"]("stand up", "*/5 * * * *")["entry"]
    assert entry == {
        "id": entry["id"],
        "kind": "cron",
        "state": "active",
        "owner": SID,
        "payload": "stand up",
        "due": 1200.0,
        "targets": [{"kind": "broadcast", "channel": None}],
        "expression": "*/5 * * * *",
        "timezone": "UTC",
    }
    zoned = tools["cron_add"]("nine", "0 9 * * *", target="swarm", name="sw", timezone="Europe/Madrid")["entry"]
    assert (zoned["due"], zoned["targets"]) == (8 * 3600.0, [{"kind": "swarm", "slug": "sw"}])
    assert tools["cron_list"]() == {"success": True, "entries": [entry, zoned], "count": 2}
    assert tools["cron_remove"](entry["id"]) == {"success": True, "entry": {**entry, "state": "cancelled"}}
    assert tools["cron_list"]() == {"success": True, "entries": [zoned], "count": 1}
    assert tools["cron_remove"]("nope") == {"success": False, "error": "no cron nope"}


def test_cron_add_reports_a_bad_expression(tools, logged):
    assert tools["cron_add"]("x", "* *") == {"success": False, "error": "a cron expression needs five fields"}
    assert logged == [("MCP cron_add failed", {"error": "a cron expression needs five fields"})]


def test_a_named_target_that_does_not_fit_is_refused(tools):
    assert tools["timer_add"]("x", delay="5", target="fleet", name="sw") == {
        "success": False,
        "error": "the fleet target takes no name",
    }
    assert tools["cron_add"]("x", "* * * * *", target="session") == {
        "success": False,
        "error": "the session target needs a name",
    }
    assert tools["stopwatch_start"]("x", "5", target="swarm", name="nope") == {
        "success": False,
        "error": "no swarm nope",
    }


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("timer_add", ("x",)),
        ("timer_list", ()),
        ("timer_cancel", ("id",)),
        ("stopwatch_start", ("x", "5")),
        ("stopwatch_stop", ("id",)),
        ("stopwatch_show", ("id",)),
        ("cron_add", ("x", "* * * * *")),
        ("cron_list", ()),
        ("cron_remove", ("id",)),
    ],
)
def test_each_tool_logs_its_own_failure(tools, logged, monkeypatch, tool, args):
    def down():
        raise RuntimeError("redis down")

    monkeypatch.setattr(commands, "connect", down)
    kwargs = {"delay": "5"} if tool == "timer_add" else {}
    assert tools[tool](*args, **kwargs) == {"success": False, "error": "redis down"}
    assert logged == [(f"MCP {tool} failed", {"error": "redis down"})]


def test_a_passed_session_id_owns_each_kind_without_an_agent_name(tools):
    assert tools["timer_add"]("x", delay="5", session_id="sid-given")["entry"]["owner"] == "sid-given"
    assert tools["stopwatch_start"]("x", "5", session_id="sid-given")["entry"]["owner"] == "sid-given"
    assert tools["cron_add"]("x", "* * * * *", session_id="sid-given")["entry"]["owner"] == "sid-given"
