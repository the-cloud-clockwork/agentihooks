import json
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from scripts import herdr_host, herdr_panes, init_agent


class FakeHerdr:
    def __init__(self, workspaces=()):
        self.calls: list[list[str]] = []
        self.workspaces = [{"workspace_id": wid, "label": label} for wid, label in workspaces]

    def __call__(self, args, environ):
        self.calls.append(list(args))
        pane = {"workspace_id": "w9", "tab_id": "w9:t2", "pane_id": "w9:p4"}
        if args[:2] == ["workspace", "list"]:
            return {"workspaces": self.workspaces}
        if args[:2] in (["tab", "create"], ["workspace", "create"]):
            return {"root_pane": pane}
        if args[:2] == ["pane", "split"]:
            return {"pane": pane}
        return {}

    def made(self, verb: str) -> list[str]:
        return next(call for call in self.calls if call[:2] == verb.split())


@pytest.fixture
def herdr(monkeypatch):
    fake = FakeHerdr(workspaces=[("w1", "agentihooks"), ("w2", "crew-a")])
    monkeypatch.setattr(herdr_host, "_cli", fake)
    monkeypatch.setattr(herdr_host, "repo_label", lambda directory: "agentihooks")
    return fake


def test_a_tab_opens_in_the_callers_workspace(herdr, tmp_path):
    placed = herdr_host.open_pane(tmp_path, "eng-a", {"HERDR_AGENT": "claude"}, "tab", "", {"HERDR_WORKSPACE_ID": "w5"})
    call = herdr.made("tab create")
    assert call[call.index("--workspace") + 1] == "w5"
    assert call[call.index("--label") + 1] == "eng-a"
    assert "HERDR_AGENT=claude" in call and "--no-focus" in call
    assert placed == herdr_host.Placement("w9", "w9:t2", "w9:p4")


def test_a_crew_tab_opens_in_the_crew_workspace(herdr, tmp_path):
    herdr_host.open_pane(tmp_path, "eng-a", {}, "tab", "crew-a", {"HERDR_WORKSPACE_ID": "w5"})
    call = herdr.made("tab create")
    assert call[call.index("--workspace") + 1] == "w2"


def test_outside_herdr_a_tab_opens_in_the_repository_workspace(herdr, tmp_path):
    herdr_host.open_pane(tmp_path, "eng-a", {}, "tab", "", {})
    call = herdr.made("tab create")
    assert call[call.index("--workspace") + 1] == "w1"


def test_a_command_tab_opens_in_the_crew_workspace_and_runs_only_that_command(herdr, tmp_path, monkeypatch):
    argv = ["codex", "resume", "--remote", "unix:///run/exe 1/codex.sock", "thread-1"]
    environ = {"HERDR_WORKSPACE_ID": "w5"}
    environs = []

    def recording(args, given):
        environs.append(given)
        return herdr(args, given)

    monkeypatch.setattr(herdr_host, "_cli", recording)
    placed = herdr_host.open_command(tmp_path, "sw-master-1", argv, "crew-a", environ)
    assert len(environs) == len(herdr.calls)
    assert len(environs) >= 2
    assert all(given is environ for given in environs)
    call = herdr.made("tab create")
    assert call[call.index("--workspace") + 1] == "w2"
    assert call[call.index("--label") + 1] == "sw-master-1"
    assert call[call.index("--cwd") + 1] == str(tmp_path)
    assert herdr.made("pane run") == ["pane", "run", "w9:p4", shlex.join(argv)]
    assert placed == herdr_host.Placement("w9", "w9:t2", "w9:p4")


def test_a_missing_workspace_is_created_with_its_label(herdr, tmp_path):
    herdr_host.open_pane(tmp_path, "eng-a", {}, "tab", "crew-b", {})
    call = herdr.made("workspace create")
    assert call[call.index("--label") + 1] == "crew-b"
    assert not any(c[:2] == ["tab", "create"] for c in herdr.calls)


def test_a_split_needs_a_calling_pane(herdr, tmp_path):
    with pytest.raises(herdr_host.HerdrError, match="HERDR_PANE_ID"):
        herdr_host.open_pane(tmp_path, "eng-a", {}, "split", "", {})
    herdr_host.open_pane(tmp_path, "eng-a", {}, "split", "", {"HERDR_PANE_ID": "w1:p1"})
    assert herdr.made("pane split")[2] == "w1:p1"


def test_the_launcher_replaces_the_pane_shell(herdr, tmp_path):
    herdr_host.run("w9:p4", tmp_path / "it's.sh", {})
    assert herdr.made("pane run")[2:] == ["w9:p4", f"exec '{tmp_path}/it'\"'\"'s.sh'"]


def test_agent_names_fit_herdrs_pattern():
    assert herdr_host.agent_name("Eng A.1") == "eng-a-1"
    assert herdr_host.agent_name("42-handoff") == "a-42-handoff"
    assert len(herdr_host.agent_name("x" * 50)) == 32


def test_long_agent_names_stay_unique_and_keep_the_lane_and_number():
    slug = "okay-we-re-going-to-mossy-rabin-2026-10-05"
    names = [herdr_host.agent_name(f"{slug}-{seat}") for seat in ("master-1", "eng-2", "eng-3", "eng-12", "ci-1")]
    assert len(set(names)) == len(names)
    assert all(len(n) <= 32 and re.fullmatch(r"[a-z][a-z0-9_-]*", n) for n in names)
    assert names[1].endswith("-eng-2") and names[3].endswith("-eng-12") and names[0].endswith("-master-1")
    assert herdr_host.agent_name(f"{slug}-eng-2") != herdr_host.agent_name(f"{slug}x-eng-2")


def test_the_spawn_rename_uses_the_name_the_swarm_looks_agents_up_by(herdr):
    from scripts.swarm.runtime import herdr_target

    name = "okay-we-re-going-to-mossy-rabin-2026-10-05-eng-4"
    assert herdr_host.rename_agent("w1:p5", name, {}) is True
    assert herdr.made("agent rename")[2:] == ["w1:p5", herdr_target(name)]


@pytest.mark.parametrize(
    ("flag", "env", "binary", "state", "expected"),
    [
        ("native", {}, "/bin/herdr", None, ("native", True)),
        ("", {"AGENTIHOOKS_TERMINAL_HOST": "native"}, "/bin/herdr", None, ("native", True)),
        ("", {}, "/bin/herdr", None, ("herdr", False)),
        ("", {}, "/bin/herdr", {"herdr": {"enabled": False}}, ("native", False)),
        ("", {}, None, None, ("native", False)),
    ],
)
def test_host_selection(monkeypatch, tmp_path, flag, env, binary, state, expected):
    monkeypatch.setattr(herdr_host, "binary", lambda: binary)
    if state is not None:
        (tmp_path / "state.json").write_text(json.dumps(state))
    assert init_agent._select_host(flag, {"AGENTIHOOKS_HOME": str(tmp_path), **env}) == expected


def _main(monkeypatch, tmp_path, *extra, agent="claude"):
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda requested, environ: (agent, "rotation"))
    monkeypatch.setattr(herdr_host, "binary", lambda: "/bin/herdr")
    monkeypatch.setattr(herdr_host, "ensure_server", lambda environ: False)
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        init_agent,
        "_launch_command",
        lambda launcher, directory, title, environ: ("linux", ["/usr/bin/terminal", str(launcher)]),
    )
    popened = []

    def popen(command, **kwargs):
        if command[0] != "/usr/bin/terminal":
            raise OSError(f"unexpected process {command[0]}")
        popened.append(command)
        Path(command[-1]).with_suffix(".started").touch()

    monkeypatch.setattr(init_agent.subprocess, "Popen", popen)
    env = {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "rt"), "AGENTIHOOKS_HOME": str(tmp_path)}
    args = ["--dir", str(tmp_path), "--name", "eng-a", "--start-timeout", "0.5", "--route-timeout", "0", *extra]
    return init_agent.main(args, env), popened


def test_init_agent_starts_the_launcher_in_a_herdr_pane(monkeypatch, tmp_path, capsys):
    ran = []
    monkeypatch.setattr(herdr_host, "open_pane", lambda *a: herdr_host.Placement("w1", "w1:t3", "w1:p7"))
    monkeypatch.setattr(
        herdr_host, "run", lambda pane, launcher, environ: (ran.append(pane), launcher.with_suffix(".started").touch())
    )

    rc, popened = _main(monkeypatch, tmp_path)

    out = capsys.readouterr().out
    assert rc == 0 and ran == ["w1:p7"] and not popened
    assert "host=herdr" in out and "pane_id=w1:p7" in out and "tab_id=w1:t3" in out


def test_an_automatic_herdr_failure_falls_back_to_a_native_terminal(monkeypatch, tmp_path, capsys):
    def broken(*a):
        raise herdr_host.HerdrError("server gone")

    monkeypatch.setattr(herdr_host, "open_pane", broken)

    rc, popened = _main(monkeypatch, tmp_path)

    out = capsys.readouterr().out
    assert rc == 0 and len(popened) == 1
    assert "host=linux" in out and "herdr_error=server gone" in out


def test_an_explicit_herdr_failure_fails_the_launch(monkeypatch, tmp_path, capsys):
    def broken(*a):
        raise herdr_host.HerdrError("server gone")

    monkeypatch.setattr(herdr_host, "open_pane", broken)

    rc, popened = _main(monkeypatch, tmp_path, "--host", "herdr")

    assert rc == 2 and not popened
    assert "server gone" in capsys.readouterr().err


def test_a_command_that_prints_nothing_succeeds(monkeypatch):
    monkeypatch.setattr(herdr_host, "binary", lambda: "/bin/herdr")
    monkeypatch.setattr(
        herdr_host.subprocess, "run", lambda *a, **k: herdr_host.subprocess.CompletedProcess(a, 0, "", "")
    )
    assert herdr_host._cli(["pane", "run", "w1:p1", "x"], {}) == {}


def test_a_server_error_is_raised_with_its_message(monkeypatch):
    monkeypatch.setattr(herdr_host, "binary", lambda: "/bin/herdr")
    reply = json.dumps({"error": {"code": "pane_not_found", "message": "no pane w1:p9"}})
    monkeypatch.setattr(
        herdr_host.subprocess, "run", lambda *a, **k: herdr_host.subprocess.CompletedProcess(a, 1, "", reply)
    )
    with pytest.raises(herdr_host.HerdrError, match="no pane w1:p9"):
        herdr_host._cli(["pane", "close", "w1:p9"], {})


def test_the_agent_is_named_in_herdr_once_it_reports_its_route(monkeypatch, tmp_path, capsys):
    renamed = []
    monkeypatch.setattr(herdr_host, "open_pane", lambda *a: herdr_host.Placement("w1", "w1:t3", "w1:p7"))

    def run(pane, launcher, environ):
        launcher.with_suffix(".started").touch()
        launcher.with_suffix(".route").write_text("status=routed\naccount=ncgma\n")

    monkeypatch.setattr(herdr_host, "run", run)
    monkeypatch.setattr(herdr_host, "rename_agent", lambda pane, name, environ: renamed.append((pane, name)) or True)

    rc, _ = _main(monkeypatch, tmp_path)

    out = capsys.readouterr().out
    assert rc == 0 and renamed == [("w1:p7", "eng-a")]
    assert "account=ncgma" in out and "agent_name=eng-a" in out


@pytest.mark.parametrize(
    ("agent", "detected", "typed", "reported"),
    [
        ("codex", True, "named", "session_name=eng-a"),
        ("codex", True, "unconfirmed", "session_name=unset"),
        ("codex", False, "named", None),
        ("claude", True, "named", None),
    ],
)
def test_a_codex_launch_names_its_session_after_herdr_detects_it(
    monkeypatch, tmp_path, capsys, agent, detected, typed, reported
):
    named = []
    monkeypatch.setattr(herdr_host, "open_pane", lambda *a: herdr_host.Placement("w1", "w1:t3", "w1:p7"))

    def run(pane, launcher, environ):
        launcher.with_suffix(".started").touch()
        launcher.with_suffix(".route").write_text("status=routed\naccount=alpha\n")

    monkeypatch.setattr(herdr_host, "run", run)
    monkeypatch.setattr(herdr_host, "rename_agent", lambda pane, name, environ: detected)
    monkeypatch.setattr(herdr_host, "wait_ready", lambda pane, environ, timeout: True)
    monkeypatch.setattr(
        herdr_host,
        "name_session",
        lambda pane, name, environ, confirmed: named.append((pane, name, environ["HOME"])) or typed,
    )

    rc, _ = _main(monkeypatch, tmp_path, agent=agent)

    lines = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert [line for line in lines if line.startswith("session_name=")] == ([reported] if reported else [])
    assert named == ([("w1:p7", "eng-a", str(tmp_path))] if reported else [])


def _codex_launch(monkeypatch, tmp_path, pane, *extra, route="status=routed\naccount=alpha\n"):
    opened = []

    def open_pane(directory, title, env, placement, workspace, environ):
        opened.append(env)
        return herdr_host.Placement("w1", "w1:t3", "w1:p7")

    def run(pane_id, launcher, environ):
        opened.append(launcher.read_text())
        launcher.with_suffix(".started").touch()
        if route:
            launcher.with_suffix(".route").write_text(route)

    monkeypatch.setattr(herdr_host, "open_pane", open_pane)
    monkeypatch.setattr(herdr_host, "run", run)
    pane.attach(monkeypatch)
    pane.check = lambda environ: environ.get("HOME") == str(tmp_path)
    Clock(monkeypatch)

    def codex_named(environ, name, since):
        opened.append((environ.get("HOME"), name, since))
        return lambda: True

    monkeypatch.setattr(init_agent, "_codex_named", codex_named)
    rc, _ = _main(monkeypatch, tmp_path, *extra, agent="codex")
    return rc, opened


TYPED = (["pane", "run"], ["agent", "prompt"], ["pane", "send-keys"])


def test_a_codex_launch_renames_at_the_ready_prompt_before_its_opening_prompt(monkeypatch, tmp_path, capsys):
    pane = CodexPane(["working", "done"])
    rc, opened = _codex_launch(monkeypatch, tmp_path, pane, "--prompt", "Start the task.")

    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert [call for call in pane.calls if call[:2] in TYPED] == [
        ["pane", "run", "w1:p7", "/rename eng-a"],
        ["agent", "prompt", "w1:p7", "Start the task."],
    ]
    assert "session_name=eng-a" in out and "opening_prompt=sent" in out
    env, launcher, named = opened
    launcher_at = next(line.split("=", 1)[1] for line in out if line.startswith("launcher_at="))
    assert named == (str(tmp_path), "eng-a", int(launcher_at) / 1000)
    assert env == {"HERDR_AGENT": "codex", init_agent.PROMPT_AFTER_START: "1"}
    assert f'set --; [ -n "${{{init_agent.PROMPT_AFTER_START}:-}}" ] || set -- ' in launcher
    assert [r.session_name for r in herdr_panes.load({}) if r.pane_id == "w1:p7"] == ["eng-a"]


def test_a_codex_launch_never_types_into_a_running_turn(monkeypatch, tmp_path, capsys):
    pane = CodexPane(["working"])
    monkeypatch.setattr(init_agent, "CODEX_READY_S", 0)
    rc, _ = _codex_launch(monkeypatch, tmp_path, pane, "--prompt", "Start the task.")

    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert [call for call in pane.calls if call[:2] in TYPED] == []
    assert "session_name=unset" in out and "session_name_error=not-ready" in out
    assert "opening_prompt=held:not-ready" in out
    assert [r.session_name for r in herdr_panes.load({}) if r.pane_id == "w1:p7"] == ["unset:not-ready"]


def test_a_codex_launch_holds_its_prompt_from_text_already_in_the_input_line(monkeypatch, tmp_path, capsys):
    pane = CodexPane(["done"], typed="half typed")
    rc, _ = _codex_launch(monkeypatch, tmp_path, pane, "--prompt", "Start the task.")

    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert [call for call in pane.calls if call[:2] in TYPED] == []
    assert "session_name_error=refused-typed" in out and "opening_prompt=held:typed" in out
    assert pane.input == "half typed"


@pytest.mark.parametrize(("route", "prompted"), [("", True), ("status=failed\nerror=no account\n", False)])
def test_a_held_prompt_goes_out_while_the_route_is_pending_but_not_after_it_failed(
    monkeypatch, tmp_path, capsys, route, prompted
):
    pane = CodexPane(["done"])
    rc, _ = _codex_launch(monkeypatch, tmp_path, pane, "--prompt", "Start the task.", route=route)

    capsys.readouterr()
    assert rc == 0
    sent = [call for call in pane.calls if call[:2] == ["agent", "prompt"]]
    assert sent == ([["agent", "prompt", "w1:p7", "Start the task."]] if prompted else [])


def test_a_codex_launch_without_a_prompt_only_renames(monkeypatch, tmp_path, capsys):
    pane = CodexPane(["done"])
    rc, opened = _codex_launch(monkeypatch, tmp_path, pane)

    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert [call for call in pane.calls if call[:2] in TYPED] == [["pane", "run", "w1:p7", "/rename eng-a"]]
    assert not [line for line in out if line.startswith("opening_prompt=")]
    assert opened[0] == {"HERDR_AGENT": "codex"}


def test_a_channel_launch_reports_the_warning_answered_in_its_pane(monkeypatch, tmp_path, capsys):
    answered = []
    monkeypatch.setattr(herdr_host, "open_pane", lambda *a: herdr_host.Placement("w1", "w1:t3", "w1:p7"))

    def run(pane, launcher, environ):
        launcher.with_suffix(".started").touch()
        launcher.with_suffix(".route").write_text("status=routed\naccount=ncgma\n")

    monkeypatch.setattr(herdr_host, "run", run)
    monkeypatch.setattr(herdr_host, "rename_agent", lambda pane, name, environ: True)
    monkeypatch.setattr(herdr_host, "answer", lambda pane, text, environ, ms: answered.append((pane, environ)) or True)

    rc, _ = _main(monkeypatch, tmp_path, "--agent", "claude", "--inbox-channel")

    assert rc == 0 and "channel_warning=answered" in capsys.readouterr().out.splitlines()
    assert [(pane, environ["HOME"]) for pane, environ in answered] == [("w1:p7", str(tmp_path))]


def test_renaming_waits_until_herdr_detects_the_agent(monkeypatch):
    attempts = []

    def cli(args, environ):
        attempts.append(args)
        if len(attempts) < 3:
            raise herdr_host.HerdrError("herdr agent rename: no agent in pane")
        return {}

    monkeypatch.setattr(herdr_host, "_cli", cli)
    monkeypatch.setattr(herdr_host.time, "sleep", lambda seconds: None)
    assert herdr_host.rename_agent("w1:p1", "probe-codex", {}) is True
    assert len(attempts) == 3


def test_renaming_gives_up_when_no_agent_appears(monkeypatch):
    def cli(args, environ):
        raise herdr_host.HerdrError("herdr agent rename: no agent in pane")

    monkeypatch.setattr(herdr_host, "_cli", cli)
    assert herdr_host.rename_agent("w1:p1", "probe-codex", {}, timeout=0) is False


def test_answer_waits_for_the_text_then_presses_enter(monkeypatch):
    calls = []
    monkeypatch.setattr(herdr_host, "_cli", lambda args, environ: calls.append((args, environ)) or {})
    assert herdr_host.answer("w1:p2", "local development", {"K": "v"}, 5000)
    assert calls == [
        (["pane", "wait-output", "w1:p2", "--match", "local development", "--timeout", "5000"], {"K": "v"}),
        (["pane", "send-keys", "w1:p2", "Enter"], {"K": "v"}),
    ]


def test_answer_presses_nothing_when_the_text_never_shows(monkeypatch):
    calls = []

    def cli(args, environ):
        calls.append(args)
        raise herdr_host.HerdrError("timed out")

    monkeypatch.setattr(herdr_host, "_cli", cli)
    assert not herdr_host.answer("w1:p2", "local development", {}, 5000)
    assert [c[1] for c in calls] == ["wait-output"]


class CodexPane:
    """A herdr pane running Codex: its status reads in order, its input line, and every call made to it."""

    def __init__(self, statuses, typed="", takes=True):
        self.statuses = list(statuses)
        self.input = typed
        self.takes = takes
        self.calls = []
        self.check = lambda environ: environ is ENV

    def __call__(self, args, environ):
        assert self.check(environ), environ
        self.calls.append(args)
        if args[:2] == ["agent", "get"]:
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return {"agent": {"agent": "codex", "agent_status": status}}
        if args[:2] == ["pane", "read"]:
            return {"text": f"› {self.input}\n"}
        if args[:2] == ["pane", "run"]:
            self.input = "" if self.takes else args[3]
        if args[:2] == ["pane", "send-keys"] and args[3:] == ["ctrl+u"]:
            self.input = ""
        return {}

    def attach(self, monkeypatch):
        monkeypatch.setattr(herdr_host, "_cli", self)
        monkeypatch.setattr(
            herdr_host,
            "_read",
            lambda pane_id, environ: self(
                ["pane", "read", pane_id, "--source", "visible", "--format", "ansi"], environ
            )["text"],
        )


class Clock:
    """A monotonic clock that moves only when the code under test sleeps."""

    def __init__(self, monkeypatch, now=100.0):
        self.now = now
        self.slept = []
        monkeypatch.setattr(herdr_host.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(herdr_host.time, "sleep", self.sleep)

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


READ = ["pane", "read", "w1:p2", "--source", "visible", "--format", "ansi"]
ENV = {"K": "v"}


@pytest.mark.parametrize("status", ["idle", "done"])
def test_name_session_renames_at_the_ready_prompt_and_confirms_the_name(monkeypatch, status):
    pane = CodexPane([status])
    pane.attach(monkeypatch)

    def confirmed():
        return ["pane", "run", "w1:p2", "/rename engineer@abc-0001"] in pane.calls

    assert herdr_host.name_session("w1:p2", "engineer@abc-0001", ENV, confirmed) == "named"
    assert pane.calls == [["agent", "get", "w1:p2"], READ, ["pane", "run", "w1:p2", "/rename engineer@abc-0001"]]
    assert pane.input == ""


@pytest.mark.parametrize("status", ["working", "blocked", "unknown"])
def test_name_session_refuses_while_a_turn_runs(monkeypatch, status):
    pane = CodexPane([status])
    pane.attach(monkeypatch)
    assert herdr_host.name_session("w1:p2", "engineer@abc-0001", ENV, lambda: True) == f"refused-{status}"
    assert pane.calls == [["agent", "get", "w1:p2"]]


def test_name_session_leaves_text_someone_typed_alone(monkeypatch):
    pane = CodexPane(["idle"], typed="half typed")
    pane.attach(monkeypatch)
    assert herdr_host.name_session("w1:p2", "engineer@abc-0001", ENV, lambda: True) == "refused-typed"
    assert pane.calls == [["agent", "get", "w1:p2"], READ]
    assert pane.input == "half typed"


def test_a_rename_that_did_not_take_is_cleared_from_the_input_line(monkeypatch):
    pane = CodexPane(["done"], takes=False)
    pane.attach(monkeypatch)
    clock = Clock(monkeypatch)
    outcome = herdr_host.name_session("w1:p2", "engineer@abc-0001", ENV, lambda: False, timeout=1.0)
    assert outcome == "unconfirmed"
    assert pane.calls == [
        ["agent", "get", "w1:p2"],
        READ,
        ["pane", "run", "w1:p2", "/rename engineer@abc-0001"],
        ["pane", "send-keys", "w1:p2", "ctrl+u"],
    ]
    assert pane.input == "" and clock.slept == [0.5, 0.5]


def test_a_rename_is_confirmed_by_polling_until_the_name_shows(monkeypatch):
    pane = CodexPane(["done"])
    slept = []
    pane.attach(monkeypatch)
    monkeypatch.setattr(herdr_host.time, "sleep", slept.append)
    checks = iter([False, False, True])
    assert herdr_host.name_session("w1:p2", "engineer@abc-0001", ENV, lambda: next(checks)) == "named"
    assert slept == [herdr_host.RENAME_POLL_S, herdr_host.RENAME_POLL_S]
    assert herdr_host.RENAME_POLL_S == 0.5 and herdr_host.RENAME_CONFIRM_S == 15.0


@pytest.mark.parametrize("error", [herdr_host.HerdrError("no pane"), subprocess.TimeoutExpired("herdr", 5)])
def test_name_session_reports_a_pane_it_could_not_type_into(monkeypatch, error):
    pane = CodexPane(["idle"])

    def cli(args, environ):
        if args[:2] == ["pane", "run"]:
            pane.input = args[3]
            raise error
        return pane(args, environ)

    pane.attach(monkeypatch)
    monkeypatch.setattr(herdr_host, "_cli", cli)
    assert herdr_host.name_session("w1:p2", "engineer@abc-0001", ENV, lambda: True) == "error"
    assert pane.calls[-1] == ["pane", "send-keys", "w1:p2", "ctrl+u"] and pane.input == ""


def test_a_failed_read_before_the_rename_types_and_clears_nothing(monkeypatch):
    pane = CodexPane(["idle"])
    pane.attach(monkeypatch)

    def read(pane_id, environ):
        raise herdr_host.HerdrError("herdr pane read: gone")

    monkeypatch.setattr(herdr_host, "_read", read)
    assert herdr_host.name_session("w1:p2", "engineer@abc-0001", ENV, lambda: True) == "error"
    assert pane.calls == [["agent", "get", "w1:p2"]]


@pytest.mark.parametrize("error", [herdr_host.HerdrError("gone"), subprocess.TimeoutExpired("herdr", 5)])
def test_a_clear_herdr_refuses_still_reports_the_error(monkeypatch, error):
    pane = CodexPane(["idle"])

    def cli(args, environ):
        reply = pane(args, environ)
        if args[:2] in (["pane", "run"], ["pane", "send-keys"]):
            raise error
        return reply

    pane.attach(monkeypatch)
    monkeypatch.setattr(herdr_host, "_cli", cli)
    assert herdr_host.name_session("w1:p2", "engineer@abc-0001", ENV, lambda: True) == "error"
    assert pane.calls[-1] == ["pane", "send-keys", "w1:p2", "ctrl+u"]


@pytest.mark.parametrize("error", [herdr_host.HerdrError("no agent"), subprocess.TimeoutExpired("herdr", 5)])
def test_an_agent_herdr_cannot_read_has_an_unknown_status(monkeypatch, error):
    def cli(args, environ):
        raise error

    monkeypatch.setattr(herdr_host, "_cli", cli)
    assert herdr_host.agent_status("w1:p2", {}) == "unknown"


def test_an_agent_record_without_a_status_reads_unknown(monkeypatch):
    monkeypatch.setattr(herdr_host, "_cli", lambda args, environ: {"agent": {"agent": "codex"}})
    assert herdr_host.agent_status("w1:p2", {}) == "unknown"


def test_the_input_line_is_read_from_the_raw_screen_herdr_prints(monkeypatch):
    ran = []

    def run(args, **kwargs):
        ran.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "Filler that is not JSON\n› /rename eng-a\n", "")

    monkeypatch.setattr(herdr_host, "binary", lambda: "/bin/herdr")
    monkeypatch.setattr(herdr_host.subprocess, "run", run)
    assert herdr_host.typed("w1:p2", {"K": "v"}) == "/rename eng-a"
    assert ran == [
        (
            ["/bin/herdr", "pane", "read", "w1:p2", "--source", "visible", "--format", "ansi"],
            {"capture_output": True, "text": True, "env": {"K": "v"}, "timeout": 30},
        )
    ]


def test_a_pane_herdr_cannot_read_is_an_error(monkeypatch):
    monkeypatch.setattr(herdr_host, "binary", lambda: "/bin/herdr")
    monkeypatch.setattr(
        herdr_host.subprocess, "run", lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", " no pane \n")
    )
    with pytest.raises(herdr_host.HerdrError) as raised:
        herdr_host.typed("w1:p2", {})
    assert str(raised.value) == "herdr pane read: no pane"


def test_reading_a_pane_without_herdr_is_an_error(monkeypatch):
    monkeypatch.setattr(herdr_host, "binary", lambda: None)
    with pytest.raises(herdr_host.HerdrError) as raised:
        herdr_host.typed("w1:p2", {})
    assert str(raised.value) == "herdr is not installed"


def test_ready_needs_two_ready_reads_in_a_row_because_codex_reads_idle_before_its_startup(monkeypatch):
    pane = CodexPane(["unknown", "idle", "working", "working", "done", "done"])
    pane.attach(monkeypatch)
    clock = Clock(monkeypatch)
    assert herdr_host.wait_ready("w1:p2", ENV, 60) is True
    assert pane.calls == [["agent", "get", "w1:p2"]] * 6
    assert clock.slept == [herdr_host.READY_SETTLE_S] * 5 and herdr_host.READY_SETTLE_S == 1.5


def test_ready_gives_up_at_its_deadline(monkeypatch):
    pane = CodexPane(["working"])
    pane.attach(monkeypatch)
    clock = Clock(monkeypatch)
    assert herdr_host.wait_ready("w1:p2", ENV, 3.0) is False
    assert pane.calls == [["agent", "get", "w1:p2"]] * 3 and clock.slept == [1.5, 1.5]


def test_ready_with_no_time_reads_once(monkeypatch):
    pane = CodexPane(["working"])
    pane.attach(monkeypatch)
    clock = Clock(monkeypatch)
    assert herdr_host.wait_ready("w1:p2", ENV, 0) is False
    assert pane.calls == [["agent", "get", "w1:p2"]] and clock.slept == []


def test_a_first_ready_read_is_not_enough(monkeypatch):
    pane = CodexPane(["idle", "working", "done", "done"])
    pane.attach(monkeypatch)
    clock = Clock(monkeypatch)
    assert herdr_host.wait_ready("w1:p2", ENV, 60) is True
    assert pane.calls == [["agent", "get", "w1:p2"]] * 4 and clock.slept == [1.5, 1.5, 1.5]


def test_a_reply_without_an_agent_record_reads_unknown(monkeypatch):
    monkeypatch.setattr(herdr_host, "_cli", lambda args, environ: {})
    assert herdr_host.agent_status("w1:p2", ENV) == "unknown"


def test_prompt_submits_the_text_to_the_agent(monkeypatch):
    calls = []
    monkeypatch.setattr(herdr_host, "_cli", lambda args, environ: calls.append((args, environ)) or {})
    assert herdr_host.prompt("w1:p2", "line one\n\nline two", {"K": "v"}) is True
    assert calls == [(["agent", "prompt", "w1:p2", "line one\n\nline two"], {"K": "v"})]


@pytest.mark.parametrize("error", [herdr_host.HerdrError("blocked"), subprocess.TimeoutExpired("herdr", 5)])
def test_prompt_reports_a_submission_herdr_refused(monkeypatch, error):
    def cli(args, environ):
        raise error

    monkeypatch.setattr(herdr_host, "_cli", cli)
    assert herdr_host.prompt("w1:p2", "text", {}) is False


def test_ensure_server_keeps_agent_identity_out_of_the_server(monkeypatch):
    answers = iter([False, True])
    started = {}
    monkeypatch.setattr(herdr_host, "server_running", lambda environ: next(answers))
    monkeypatch.setattr(herdr_host, "binary", lambda: "/bin/herdr")
    monkeypatch.setattr(herdr_host.subprocess, "Popen", lambda args, env, **kw: started.update(env=env))
    environ = {
        "HOME": "/home/op",
        "PATH": "/usr/bin",
        "AGENTIHOOKS_PROFILE": "documentation",
        "AGENTIHOOKS_SWARM": "s1",
        "CLAUDE_CONFIG_DIR": "/home/op/.agentihooks/profiles/.homes/documentation/claude",
        "CODEX_HOME": "/x",
        "COPILOT_HOME": "/y",
    }

    assert herdr_host.ensure_server(environ) is True
    assert started["env"] == {"HOME": "/home/op", "PATH": "/usr/bin"}
    assert environ["AGENTIHOOKS_PROFILE"] == "documentation"
