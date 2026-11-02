import json

import pytest

from scripts import cli_delegates
from scripts.events.redis_state import RedisStateStore
from scripts.scheduler import cli, commands

pytestmark = pytest.mark.unit

OWNER = "engineer@323133-1958"
NOW = 1000.0


@pytest.fixture
def scheduler(monkeypatch):
    import fakeredis

    from scripts.scheduler.core import Scheduler
    from scripts.swarm.store import RedisStore, SwarmConfig

    found = Scheduler(RedisStateStore(fakeredis.FakeRedis(decode_responses=True), "scheduler"), machine="proof")
    RedisStore(found.state.redis).create(SwarmConfig("sw", "/repo", 2, 1))
    monkeypatch.setattr(commands, "connect", lambda: found)
    monkeypatch.setattr(commands.time, "time", lambda: NOW)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", OWNER)
    return found


def run(capsys, command, argv):
    code = command(argv)
    captured = capsys.readouterr()
    return code, json.loads(captured.out) if captured.out else None, captured.err


@pytest.mark.parametrize(("noun", "command"), [("timer", cli.timer), ("stopwatch", cli.stopwatch), ("cron", cli.cron)])
def test_each_noun_is_an_agentihooks_command(noun, command):
    assert cli_delegates.delegated_cli([noun, "list"]) is command


def test_timer_add_in_a_delay_reaches_the_fleet(capsys, scheduler):
    code, view, err = run(capsys, cli.timer, ["add", "--in", "5m", "--fleet", "hello", "world"])
    assert (code, err) == (0, "")
    assert view == {
        "id": view["id"],
        "kind": "timer",
        "state": "active",
        "owner": OWNER,
        "payload": "hello world",
        "due": NOW + 300,
        "targets": [{"kind": "broadcast", "channel": None}],
    }
    assert scheduler.get(view["id"]).state == "active"


def test_timer_add_at_a_time_reaches_a_session(capsys, scheduler):
    code, view, _ = run(
        capsys, cli.timer, ["add", "--at", "2026-10-18T09:30:00+00:00", "--session", "sid-1", "wake", "up"]
    )
    assert code == 0
    assert (view["due"], view["payload"]) == (1792315800.0, "wake up")
    assert view["targets"] == [{"kind": "inbox", "address": "sid-1"}]


def test_timer_list_and_cancel(capsys, scheduler):
    _, first, _ = run(capsys, cli.timer, ["add", "--in", "90", "--swarm", "sw", "one"])
    _, second, _ = run(capsys, cli.timer, ["add", "--in", "10", "--seat", "master@sw", "two"])
    code, listed, _ = run(capsys, cli.timer, ["list"])
    assert code == 0
    assert [view["id"] for view in listed] == [second["id"], first["id"]]
    assert listed[1]["targets"] == [{"kind": "swarm", "slug": "sw"}]
    code, cancelled, _ = run(capsys, cli.timer, ["cancel", second["id"]])
    assert (code, cancelled["state"]) == (0, "cancelled")
    assert [view["id"] for view in run(capsys, cli.timer, ["list"])[1]] == [first["id"]]


def test_a_refused_timer_command_says_why_and_fails(capsys, scheduler):
    assert run(capsys, cli.timer, ["cancel", "nope"]) == (1, None, "agentihooks timer: no timer nope\n")
    assert run(capsys, cli.timer, ["add", "--in", "soon", "--fleet", "x"]) == (
        1,
        None,
        "agentihooks timer: soon is not a duration such as 90, 90s, 5m or 2h\n",
    )
    assert run(capsys, cli.timer, ["add", "--in", "5", "--seat", OWNER, "x"]) == (
        1,
        None,
        f"agentihooks timer: {OWNER} is not a seat address\n",
    )
    assert run(capsys, cli.timer, ["add", "--in", "5", "--swarm", "nope", "x"]) == (
        1,
        None,
        "agentihooks timer: no swarm nope\n",
    )
    assert run(capsys, cli.timer, ["add", "--at", "2026-10-18T09:30:00", "--fleet", "x"]) == (
        1,
        None,
        "agentihooks timer: 2026-10-18T09:30:00 is not an ISO time with an offset such as 2026-10-18T09:30:00+00:00\n",
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["add", "--in", "5", "--at", "2026-10-18T09:30:00+00:00", "--fleet", "x"],
        ["add", "--fleet", "x"],
        ["add", "--in", "5", "x"],
        ["add", "--in", "5", "--fleet", "--swarm", "sw", "x"],
        ["add", "--in", "5", "--fleet"],
        ["cancel"],
        ["stop"],
    ],
)
def test_timer_usage_errors_exit_two(capsys, scheduler, argv):
    with pytest.raises(SystemExit) as exit_:
        cli.timer(argv)
    assert exit_.value.code == 2


def test_a_command_without_an_identity_is_refused(capsys, scheduler, monkeypatch):
    from scripts.inbox import cli as inbox_cli

    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(inbox_cli, "registered_name", lambda: "")
    assert run(capsys, cli.stopwatch, ["start", "--limit", "5", "--fleet", "x"]) == (
        1,
        None,
        "agentihooks stopwatch: this session has no identity: set AGENTIHOOKS_AGENT_NAME"
        " or run inside a Claude Code session\n",
    )


def test_stopwatch_start_show_and_stop(capsys, scheduler, monkeypatch):
    code, started, _ = run(capsys, cli.stopwatch, ["start", "--limit", "1m", "--seat", "master@sw", "over", "time"])
    assert code == 0
    assert started == {
        "id": started["id"],
        "kind": "stopwatch",
        "state": "active",
        "owner": OWNER,
        "payload": "over time",
        "due": NOW + 60,
        "targets": [{"kind": "inbox", "address": "master@sw"}],
        "limit": 60.0,
    }
    monkeypatch.setattr(commands.time, "time", lambda: NOW + 15)
    code, shown, _ = run(capsys, cli.stopwatch, ["show", started["id"]])
    assert (code, shown["state"], shown["elapsed"]) == (0, "active", 15)
    code, stopped, _ = run(capsys, cli.stopwatch, ["stop", started["id"]])
    assert (code, stopped["state"], stopped["elapsed"]) == (0, "stopped", 15)
    assert run(capsys, cli.stopwatch, ["stop", started["id"]]) == (
        1,
        None,
        f"agentihooks stopwatch: stopwatch {started['id']} is already stopped\n",
    )
    assert run(capsys, cli.stopwatch, ["show", "nope"]) == (1, None, "agentihooks stopwatch: no stopwatch nope\n")


def test_stopwatch_start_needs_a_limit(scheduler):
    with pytest.raises(SystemExit) as exit_:
        cli.stopwatch(["start", "--fleet", "x"])
    assert exit_.value.code == 2


def test_cron_add_list_and_remove(capsys, scheduler):
    code, added, _ = run(capsys, cli.cron, ["add", "--expr", "*/5 * * * *", "--swarm", "sw", "stand", "up"])
    assert code == 0
    assert added == {
        "id": added["id"],
        "kind": "cron",
        "state": "active",
        "owner": OWNER,
        "payload": "stand up",
        "due": 1200.0,
        "targets": [{"kind": "swarm", "slug": "sw"}],
        "expression": "*/5 * * * *",
        "timezone": "UTC",
    }
    _, zoned, _ = run(capsys, cli.cron, ["add", "--expr", "0 9 * * *", "--tz", "Europe/Madrid", "--fleet", "nine"])
    assert (zoned["timezone"], zoned["due"]) == ("Europe/Madrid", 8 * 3600.0)
    code, listed, _ = run(capsys, cli.cron, ["list"])
    assert (code, [view["id"] for view in listed]) == (0, [added["id"], zoned["id"]])
    code, removed, _ = run(capsys, cli.cron, ["remove", added["id"]])
    assert (code, removed["state"]) == (0, "cancelled")
    assert [view["id"] for view in run(capsys, cli.cron, ["list"])[1]] == [zoned["id"]]
    assert run(capsys, cli.cron, ["add", "--expr", "* *", "--fleet", "x"]) == (
        1,
        None,
        "agentihooks cron: a cron expression needs five fields\n",
    )


def test_cron_add_needs_an_expression(scheduler):
    with pytest.raises(SystemExit) as exit_:
        cli.cron(["add", "--fleet", "x"])
    assert exit_.value.code == 2


@pytest.mark.parametrize(("command", "prog"), [(cli.timer, "timer"), (cli.stopwatch, "stopwatch"), (cli.cron, "cron")])
def test_help_names_the_command_and_every_target(capsys, command, prog):
    with pytest.raises(SystemExit):
        command(["--help"])
    out = capsys.readouterr().out
    assert out.startswith(f"usage: agentihooks {prog} ")
    assert "TARGET is --fleet, --swarm SLUG, --seat SEAT@SLUG or --session NAME." in out


@pytest.mark.parametrize("command", [cli.timer, cli.stopwatch, cli.cron])
def test_a_command_without_an_action_names_the_missing_action(capsys, command):
    with pytest.raises(SystemExit) as exit_:
        command([])
    assert exit_.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1].endswith("error: the following arguments are required: action")
