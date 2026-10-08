import json
import re
from pathlib import Path

import pytest

from scripts import herdr_host, init_agent


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


def _main(monkeypatch, tmp_path, *extra):
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda requested, environ: ("claude", "rotation"))
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
