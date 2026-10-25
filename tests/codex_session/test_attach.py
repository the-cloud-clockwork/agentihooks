import shlex
import sys
from dataclasses import replace
from unittest.mock import Mock

import pytest

from scripts import init_agent
from scripts.codex_session import attach
from scripts.codex_session.binding import CONNECTED, EXITED, FAILED, STARTING, TransportRecord

pytestmark = pytest.mark.unit

RECORD = TransportRecord(
    execution_id="exe-1",
    execution_generation=1,
    seat="master@sw",
    agent_name="sw-master-1",
    task_id="master",
    endpoint_ref="unix:///run/exe-1/codex.sock",
    thread_id="thread-1",
    state=CONNECTED,
)


class Clock:
    def __init__(self, now=0.0):
        self.now, self.slept = now, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def reads(*records):
    rows = list(records)
    return lambda: rows.pop(0) if len(rows) > 1 else rows[0]


def test_the_viewer_resumes_the_recorded_thread_over_the_recorded_endpoint():
    assert attach.viewer_argv("codex", RECORD) == [
        "codex",
        "-c",
        "check_for_update_on_startup=false",
        "resume",
        "--remote",
        "unix:///run/exe-1/codex.sock",
        "thread-1",
    ]


@pytest.mark.parametrize(
    ("record", "ready"),
    [
        (RECORD, True),
        (None, False),
        (replace(RECORD, state=STARTING), False),
        (replace(RECORD, thread_id=""), False),
    ],
)
def test_only_a_connected_execution_with_a_thread_is_attachable(record, ready):
    assert attach.attachable(record) is ready


def test_the_viewer_waits_until_the_thread_is_connected():
    clock = Clock()
    found = attach.wait_attachable(reads(None, replace(RECORD, state=STARTING), RECORD), 10, clock, clock.sleep)
    assert found == RECORD
    assert clock.slept == [0.5, 0.5]


@pytest.mark.parametrize("state", [FAILED, EXITED])
def test_an_ended_execution_refuses_the_viewer(state):
    clock = Clock()
    with pytest.raises(attach.AttachError) as refused:
        attach.wait_attachable(reads(replace(RECORD, state=state)), 10, clock, clock.sleep)
    assert str(refused.value) == f"the execution's app-server {state}; there is no thread to attach"
    assert clock.slept == []


def test_a_thread_that_never_connects_times_out():
    clock = Clock()
    with pytest.raises(attach.AttachError) as refused:
        attach.wait_attachable(reads(None), 1.5, clock, clock.sleep)
    assert str(refused.value) == "the execution has no connected thread after 1.5 seconds"
    assert clock.now == 1.5


def test_a_viewer_that_ran_and_closed_returns_without_another_attach():
    clock, ran = Clock(100.0), []

    def run(command):
        ran.append(command)
        clock.now += 5.0
        return 1

    assert attach.attach(reads(RECORD), run, "codex", 10, clock, clock.sleep) == 1
    assert ran == [attach.viewer_argv("codex", RECORD)]


def test_a_closed_viewer_with_a_clean_exit_returns_at_once():
    clock, ran = Clock(), []
    assert attach.attach(reads(RECORD), lambda command: ran.append(command) or 0, "codex", 10, clock, clock.sleep) == 0
    assert len(ran) == 1 and clock.slept == []


def test_a_viewer_refused_before_the_first_rollout_is_retried_a_bounded_number_of_times():
    clock, ran = Clock(100.0), []
    assert attach.attach(reads(RECORD), lambda command: ran.append(command) or 2, "codex", 10, clock, clock.sleep) == 2
    assert len(ran) == 3
    assert clock.slept == [0.5] * 3


def test_a_viewer_that_attaches_on_its_second_try_keeps_running():
    clock, codes = Clock(100.0), [2, 0]
    assert attach.attach(reads(RECORD), lambda command: codes.pop(0), "codex", 10, clock, clock.sleep) == 0
    assert codes == []


def test_each_attach_waits_for_the_thread_with_the_given_sleep():
    clock = Clock(100.0)
    assert attach.attach(reads(None, RECORD), lambda command: 0, "codex", 10, clock, clock.sleep) == 0
    assert clock.slept == [0.5]


def test_main_attaches_the_named_agent_with_the_given_binary():
    ran = []
    assert (
        attach.main(
            ["--slug", "sw", "--agent", "sw-master-1", "--codex-bin", "/bin/codex"],
            reads(RECORD),
            lambda c: ran.append(c) or 0,
        )
        == 0
    )
    assert ran == [attach.viewer_argv("/bin/codex", RECORD)]


def test_main_reports_a_refused_attach(capsys):
    code = attach.main(["--slug", "sw", "--agent", "a", "--timeout", "0"], reads(None), lambda c: 0)
    assert code == 3
    assert (
        capsys.readouterr().err == "agentihooks codex-session: the execution has no connected thread after 0 seconds\n"
    )


def test_the_parser_defaults():
    parser = attach._parser()
    args = parser.parse_args(["--slug", "sw", "--agent", "a"])
    assert (args.codex_bin, args.timeout) == ("codex", 120.0)
    assert parser.prog == "python -m scripts.codex_session attach"


@pytest.mark.parametrize("argv", [["--agent", "a"], ["--slug", "sw"]])
def test_the_parser_needs_the_slug_and_the_agent(argv, capsys):
    with pytest.raises(SystemExit):
        attach._parser().parse_args(argv)
    assert "the following arguments are required" in capsys.readouterr().err


def test_main_reads_the_named_agent_and_runs_the_viewer_by_default(monkeypatch):
    from types import SimpleNamespace

    asked, ran = [], []
    monkeypatch.setattr(attach, "recorded", lambda slug, name: asked.append((slug, name)) or reads(RECORD))
    monkeypatch.setattr(attach.subprocess, "run", lambda command: ran.append(command) or SimpleNamespace(returncode=0))
    assert attach.main(["--slug", "sw", "--agent", "sw-master-1"]) == 0
    assert asked == [("sw", "sw-master-1")]
    assert ran == [attach.viewer_argv("codex", RECORD)]


def test_recorded_reads_the_named_agents_current_transport(monkeypatch):
    from types import SimpleNamespace

    from scripts.swarm.store import APP_SERVER_TRANSPORT, AgentRecord

    agents = [
        AgentRecord("other", "eng", "t1", harness="codex", transport=APP_SERVER_TRANSPORT, execution_id="exe-0"),
        AgentRecord(
            "sw-master-1", "master", "master", harness="codex", transport=APP_SERVER_TRANSPORT, execution_id="exe-1"
        ),
        AgentRecord("legacy", "eng", "t2", harness="codex", execution_id="exe-2"),
        AgentRecord("launching", "eng", "t3", harness="codex"),
        AgentRecord("claude", "eng", "t4", harness="claude"),
    ]
    store = SimpleNamespace(agents=lambda slug: agents if slug == "sw" else [])
    monkeypatch.setattr("scripts.swarm.store.connect", lambda: store)
    monkeypatch.setattr(
        "scripts.swarm.live_binding.transport",
        lambda found, agent: RECORD if found is store and agent.name == "sw-master-1" else None,
    )
    assert attach.recorded("sw", "sw-master-1")() == RECORD
    assert attach.recorded("sw", "missing")() is None
    assert attach.recorded("other", "sw-master-1")() is None
    assert attach.recorded("sw", "launching")() is None
    for name in ("legacy", "claude"):
        with pytest.raises(attach.AttachError) as refused:
            attach.recorded("sw", name)()
        assert str(refused.value) == f"{name} runs no app-server a native terminal can attach to"


def test_the_app_server_pane_runs_the_server_in_its_own_session_and_the_viewer_in_front(tmp_path):
    spec = init_agent.AgentSpec(agent="codex", profile="engineer", transport=init_agent.APP_SERVER, seat="master@sw")
    launcher, opening = init_agent._write_launcher(
        tmp_path, "sw-master-1", "Open.", [], {"XDG_RUNTIME_DIR": str(tmp_path), "AGENTIHOOKS_SWARM": "sw"}, spec
    )
    lines = launcher.read_text().splitlines()
    serve = next(index for index, line in enumerate(lines) if line.startswith("setsid -f "))
    words = shlex.split(lines[serve])
    log = launcher.with_name(f"{launcher.stem}.serve.log")
    assert words[:4] == ["setsid", "-f", "sh", "-c"]
    server, _, removed = words[4].removeprefix(init_agent.LAUNCHER_PIN).partition("; rm -f ")
    assert shlex.split(server)[:5] == [sys.executable, "-P", "-m", "scripts.codex_session", "serve"]
    assert shlex.split(removed) == [str(opening), str(init_agent._route_report(launcher))]
    assert words[5:] == ["</dev/null", f">>{log}", "2>&1"]
    assert shlex.split(lines[serve + 1]) == [
        sys.executable,
        "-P",
        "-m",
        "scripts.codex_session",
        "attach",
        "--slug",
        "sw",
        "--agent",
        "sw-master-1",
    ]
    removal = next((line for line in lines[serve + 2 :] if line.startswith("rm -f ")), "")
    assert removal
    assert shlex.split(removal) == ["rm", "-f", str(launcher)]


def test_a_viewer_launched_outside_a_swarm_names_an_empty_slug(tmp_path):
    spec = init_agent.AgentSpec(agent="codex", profile="engineer", transport=init_agent.APP_SERVER, seat="master@sw")
    launcher, _ = init_agent._write_launcher(tmp_path, "a", "Open.", [], {"XDG_RUNTIME_DIR": str(tmp_path)}, spec)
    lines = launcher.read_text().splitlines()
    serve = next(index for index, line in enumerate(lines) if line.startswith("setsid -f "))
    assert shlex.split(lines[serve + 1])[4:] == ["--slug", "", "--agent", "a"]


def _agents():
    from scripts.swarm.store import APP_SERVER_TRANSPORT, AgentRecord

    push = {"harness": "codex", "transport": APP_SERVER_TRANSPORT}
    return [
        AgentRecord("sw-eng-1", "eng", "t1", execution_id="exe-0", **push),
        AgentRecord("sw-master-1", "master", "master", execution_id="exe-1", **push),
        AgentRecord("sw-eng-2", "eng", "t2", execution_id="exe-2", harness="codex"),
        AgentRecord("sw-eng-3", "eng", "t3", execution_id="exe-3", harness="claude"),
        AgentRecord("sw-eng-4", "eng", "t4", execution_id="exe-4", runtime_backend="kubernetes", **push),
    ]


@pytest.mark.parametrize(
    ("execution", "name"),
    [("exe-1", "sw-master-1"), ("exe-0", "sw-eng-1"), ("exe-2", None), ("exe-3", None), ("exe-4", None), ("x", None)],
)
def test_only_a_local_app_server_execution_reopens_in_a_native_terminal(execution, name):
    from types import SimpleNamespace

    store = SimpleNamespace(agents=lambda slug: _agents() if slug == "sw" else [])
    found = attach.reopenable(store, "sw", execution)
    assert (found and found.name) == name
    assert attach.reopenable(store, "other", execution) is None


def test_reopen_resumes_the_recorded_thread_and_never_starts_a_server(monkeypatch):
    from types import SimpleNamespace

    written, agent = [], _agents()[1]
    store = SimpleNamespace(execution_registry=SimpleNamespace(set_pane=lambda *pane: written.append(pane)))
    monkeypatch.setattr(
        "scripts.swarm.live_binding.transport",
        lambda found, seen: RECORD if (found, seen) == (store, agent) else None,
    )
    opened = []
    placed = SimpleNamespace(pane_id="w9:p4")
    result = attach.reopen(store, "sw", agent, lambda title, argv: opened.append((title, argv)) or placed, "/bin/codex")
    assert opened == [
        (
            "sw-master-1",
            [
                "/bin/codex",
                "-c",
                "check_for_update_on_startup=false",
                "resume",
                "--remote",
                RECORD.endpoint_ref,
                "thread-1",
            ],
        )
    ]
    assert not {"app-server", "serve", "scripts.codex_session"} & set(opened[0][1])
    assert result == {"agent": "sw-master-1", "thread": "thread-1", "pane": "w9:p4"}
    assert written == [("sw", "exe-1", "w9:p4")]


def test_reopen_refused_by_the_viewer_records_no_pane(monkeypatch):
    from types import SimpleNamespace

    written = []
    store = SimpleNamespace(execution_registry=SimpleNamespace(set_pane=lambda *pane: written.append(pane)))
    monkeypatch.setattr("scripts.swarm.live_binding.transport", lambda found, seen: RECORD)

    def refuse(title, argv):
        raise RuntimeError("herdr refused the pane")

    with pytest.raises(RuntimeError):
        attach.reopen(store, "sw", _agents()[1], refuse, "codex")
    assert written == []


@pytest.mark.parametrize("record", [None, replace(RECORD, state=STARTING), replace(RECORD, thread_id="")])
def test_reopen_refuses_an_execution_with_no_connected_thread(monkeypatch, record):
    from types import SimpleNamespace

    monkeypatch.setattr("scripts.swarm.live_binding.transport", lambda found, seen: record)
    opened = []
    with pytest.raises(attach.AttachError) as refused:
        attach.reopen(SimpleNamespace(), "sw", _agents()[1], lambda title, argv: opened.append(argv), "codex")
    assert str(refused.value) == "sw-master-1 has no connected thread to reopen"
    assert opened == []


@pytest.mark.parametrize(
    ("slug", "workspace"), [("proof-323133-cxc8-1", "proof-323133-cxc8-1"), ("sw", "agentihooks-323133")]
)
def test_the_local_terminal_opens_the_viewer_in_the_swarm_workspace(monkeypatch, slug, workspace):
    from pathlib import Path
    from types import SimpleNamespace

    from scripts import herdr_host

    config = SimpleNamespace(repo="/home/op/dev/agentihooks", code="323133", slug=slug)
    written = []
    store = SimpleNamespace(
        agents=lambda asked: _agents() if asked == slug else [],
        config=lambda asked: config if asked == slug else None,
        execution_registry=SimpleNamespace(set_pane=lambda *pane: written.append(pane)),
        redis=Mock(),
        key=lambda *parts: ":".join(parts),
    )
    monkeypatch.setattr("scripts.swarm.live_binding.transport", lambda found, seen: RECORD if found is store else None)
    opened = []
    monkeypatch.setattr(
        herdr_host,
        "open_command",
        lambda *call: opened.append(call) or herdr_host.Placement("w1", "w1:t1", "w1:p1", "term-1"),
    )
    environ = {"HERDR_SOCKET": "/run/herdr.sock"}
    assert attach.local_terminal(store, slug, "exe-1", environ) == {
        "agent": "sw-master-1",
        "thread": "thread-1",
        "pane": "w1:p1",
    }
    assert opened == [
        (
            Path(config.repo),
            "sw-master-1",
            attach.viewer_argv("codex", RECORD),
            workspace,
            environ,
        )
    ]
    assert written == [(slug, "exe-1", "w1:p1")]
    store.redis.hset.assert_called_once_with(f"{slug}:codex-viewers", "w1:p1", '["exe-1", "term-1"]')


def test_the_local_terminal_leaves_other_executions_to_the_worker_terminal(monkeypatch):
    from types import SimpleNamespace

    from scripts import herdr_host

    store = SimpleNamespace(agents=lambda slug: _agents(), config=lambda slug: pytest.fail("config read"))
    monkeypatch.setattr(herdr_host, "open_command", lambda *call: pytest.fail("pane opened"))
    assert attach.local_terminal(store, "sw", "exe-4", {}) is None


@pytest.mark.parametrize(
    ("launched", "viewed", "listed", "closed"),
    [
        ("term-0", "", "term-0", True),
        ("", "term-0", "term-0", True),
        ("term-0", "", "operator-shell", False),
        ("", "", "term-0", False),
    ],
)
def test_a_reopen_closes_the_earlier_launch_pane_only_on_its_recorded_terminal(
    monkeypatch, launched, viewed, listed, closed
):
    import json
    from types import SimpleNamespace

    from scripts import herdr_host, herdr_panes

    agents = [replace(agent, pane_id="w1:p0") for agent in _agents()]
    config = SimpleNamespace(repo="/home/op/dev/agentihooks", code="323133", slug="sw")
    redis = Mock()
    redis.hgetall.return_value = {"w1:p0": json.dumps(["exe-1", viewed])} if viewed else {}
    store = SimpleNamespace(
        agents=lambda asked: agents,
        config=lambda asked: config,
        execution_registry=SimpleNamespace(set_pane=lambda *pane: None),
        redis=redis,
        key=lambda *parts: ":".join(parts),
    )
    environ = {"HERDR_SOCKET": "/run/herdr.sock"}
    monkeypatch.setattr("scripts.swarm.live_binding.transport", lambda found, seen: RECORD)
    monkeypatch.setattr(
        herdr_host, "open_command", lambda *call: herdr_host.Placement("w1", "w1:t1", "w1:p1", "term-1")
    )
    records = [
        SimpleNamespace(pane_id="w1:p0", terminal_id=launched, kind="init-agent", owner_session="sw-master-1"),
        SimpleNamespace(pane_id="w1:p0", terminal_id="other", kind="init-agent", owner_session="sw-eng-9"),
        SimpleNamespace(pane_id="w1:p0", terminal_id="other", kind="run-in-terminal", owner_session="sw-master-1"),
    ]
    monkeypatch.setattr(herdr_panes, "load", lambda seen: records[: 3 if launched else 0] if seen is environ else [])
    calls = []

    def cli(args, seen):
        assert seen is environ
        calls.append(args)
        if args == ["pane", "list"]:
            return {
                "panes": [{"pane_id": "w1:p0", "terminal_id": listed}, {"pane_id": "w1:p1", "terminal_id": "term-1"}]
            }
        return {}

    monkeypatch.setattr(herdr_host, "_cli", cli)
    assert attach.local_terminal(store, "sw", "exe-1", environ)["pane"] == "w1:p1"
    assert (["pane", "close", "w1:p0"] in calls) is closed
    assert ["pane", "close", "w1:p1"] not in calls
    if closed:
        redis.hdel.assert_called_once_with("sw:codex-viewers", "w1:p0")
    else:
        redis.hdel.assert_not_called()


def test_swarm_terminal_reopens_a_local_execution_before_the_worker_terminal(monkeypatch, capsys):
    import json
    from types import SimpleNamespace

    from scripts.swarm import cli

    monkeypatch.setenv("AGENTIHOOKS_VIEWER_MARK", "seen")
    monkeypatch.setattr(
        attach,
        "local_terminal",
        lambda store, slug, execution, environ: {"pane": execution, "mark": environ["AGENTIHOOKS_VIEWER_MARK"]},
    )
    monkeypatch.setattr(
        "scripts.swarm_v2.kubernetes.operator_terminal.command", lambda *args: pytest.fail("worker terminal")
    )
    cli.cmd_terminal(object(), SimpleNamespace(slug="sw", execution="exe-1"))
    assert json.loads(capsys.readouterr().out) == {"pane": "exe-1", "mark": "seen"}


def test_swarm_terminal_falls_back_to_the_worker_terminal(monkeypatch, capsys):
    import json
    from types import SimpleNamespace

    from scripts.swarm import cli

    store = object()
    monkeypatch.setattr(attach, "local_terminal", lambda found, slug, execution, environ: None)
    monkeypatch.setattr(
        "scripts.swarm_v2.kubernetes.operator_terminal.command",
        lambda found, slug, execution, environ, who: {"target": execution} if found is store else None,
    )
    cli.cmd_terminal(store, SimpleNamespace(slug="sw", execution="exe-4"))
    assert json.loads(capsys.readouterr().out) == {"target": "exe-4"}


def test_swarm_terminal_reports_a_refused_reopen_as_a_swarm_error(monkeypatch):
    from types import SimpleNamespace

    from scripts.swarm import cli
    from scripts.swarm.store import SwarmError

    def refused(store, slug, execution, environ):
        raise attach.AttachError("sw-master-1 has no connected thread to reopen")

    monkeypatch.setattr(attach, "local_terminal", refused)
    with pytest.raises(SwarmError, match="^sw-master-1 has no connected thread to reopen$"):
        cli.cmd_terminal(object(), SimpleNamespace(slug="sw", execution="exe-1"))
