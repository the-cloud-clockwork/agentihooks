import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts import herdr_host, init_agent
from scripts.claude_config import claude_json


@pytest.fixture(autouse=True)
def _no_herdr(monkeypatch):
    monkeypatch.setattr(herdr_host, "binary", lambda: None)
    monkeypatch.setattr("scripts.profile_telemetry.installed_langfuse_env", lambda target: {})


def _profile(monkeypatch, tmp_path, target="claude"):
    from scripts import select_profile
    from scripts.profiles import binding

    home = tmp_path / "engineer" / target
    home.mkdir(parents=True, exist_ok=True)
    (home / binding.PERSONAS[target]).write_text(binding.persona("Engineer instructions.\n"))
    (home.parent / f"{target}.sources.json").write_text("[]")
    binding.write(home, "engineer", target)
    env = {"AGENTIHOOKS_PROFILE": "engineer", binding.HOMES[target]: str(home)}
    monkeypatch.setattr(select_profile, "prepare", lambda *a: (env, a[4]))
    return binding, env


def test_the_agent_flag_explains_the_rotation_default():
    [agent] = [action for action in init_agent._parser()._actions if "--agent" in action.option_strings]
    assert agent.help == init_agent.AGENT_HELP


def test_dry_run_preserves_claude_flags_and_keeps_prompt_out_of_launcher(monkeypatch, tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    runtime = tmp_path / "runtime"
    _, profile_env = _profile(monkeypatch, tmp_path)
    prompt = "apostrophe ' quote \" semicolon ; and $(command)"
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda requested, environ: ("claude", "rotation"))

    monkeypatch.setattr(
        init_agent.shutil, "which", lambda name: "/usr/bin/agentihooks" if name == "agentihooks" else None
    )
    monkeypatch.setattr(
        init_agent,
        "_launch_command",
        lambda launcher, directory, title, environ: ("linux", ["/usr/bin/terminal", str(launcher)]),
    )

    rc = init_agent.main(
        [
            "--dir",
            str(project),
            "--name",
            "quota-test",
            "--prompt",
            prompt,
            "--dry-run",
            "--",
            "--resume",
            "session-id",
            "--fork-session",
            "--model",
            "fable",
        ],
        {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(runtime), "SHELL": "/bin/bash", **profile_env},
    )

    assert rc == 0
    launcher = next((runtime / "agentihooks-claude-terminal").glob("*.sh"))
    launcher_text = launcher.read_text()
    route_report = launcher.with_suffix(".route")
    assert (
        f"/usr/bin/agentihooks select-profile engineer --agent claude -- --agentihooks-fallback-bare --agentihooks-report {route_report} --name quota-test"
    ) in launcher_text
    assert "--resume session-id --fork-session --model fable" in launcher_text
    assert prompt not in launcher_text
    prompt_file = next((runtime / "agentihooks-claude-terminal").glob("*.prompt"))
    assert prompt_file.read_text() == prompt
    assert "host=linux" in capsys.readouterr().out


def test_wsl_command_hands_windows_terminal_a_windows_resolvable_program(monkeypatch, tmp_path):
    monkeypatch.setattr(init_agent.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        init_agent.shutil,
        "which",
        lambda name: {"wt.exe": "/mnt/c/wt.exe", "wsl.exe": "/mnt/c/WINDOWS/system32/wsl.exe"}.get(name),
    )

    host, command = init_agent._launch_command(
        tmp_path / "launch.sh",
        tmp_path,
        "a;b",
        {"WSL_DISTRO_NAME": "UColt"},
    )

    assert host == "wsl"
    assert command == [
        "/mnt/c/wt.exe",
        "-w",
        "0",
        "new-tab",
        "--title",
        "a_b",
        "wsl.exe",
        "-d",
        "UColt",
        "--",
        "bash",
        "-lic",
        str(tmp_path / "launch.sh"),
    ]
    assert not any(argument.startswith("/mnt/") for argument in command[1:])


def _launch(monkeypatch, tmp_path, popen):
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda requested, environ: ("claude", "rotation"))
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        init_agent,
        "_launch_command",
        lambda launcher, directory, title, environ: ("linux", ["/usr/bin/terminal", str(launcher)]),
    )
    monkeypatch.setattr(init_agent.subprocess, "Popen", popen)
    return init_agent.main(
        [
            "--dir",
            str(tmp_path),
            "--name",
            "handshake",
            "--prompt",
            "hi",
            "--start-timeout",
            "0",
            "--route-timeout",
            "0",
            "--",
            "--model",
            "opus",
        ],
        {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "runtime")},
    )


def test_launch_succeeds_only_after_the_terminal_starts_the_launcher(monkeypatch, tmp_path, capsys):
    def popen(command, **kwargs):
        launcher = Path(command[-1])
        assert launcher.with_suffix(".started").as_posix() in launcher.read_text()
        launcher.with_suffix(".started").touch()

    rc = _launch(monkeypatch, tmp_path, popen)

    out = capsys.readouterr().out
    assert rc == 0
    assert "status=started" in out
    assert "claude_args=--model opus" in out
    assert "harness_at=" in out.splitlines()
    assert not list((tmp_path / "runtime" / "agentihooks-claude-terminal").glob("*.started"))


def test_launch_reports_when_the_launcher_and_the_harness_started(monkeypatch, tmp_path, capsys):
    def popen(command, **kwargs):
        launcher = Path(command[-1])
        launcher.with_suffix(".started").touch()
        launcher.with_suffix(".route").write_text("status=routed\naccount=a\nplacement=open\n")
        os.utime(launcher.with_suffix(".started"), (1_791_000_001.5, 1_791_000_001.5))
        os.utime(launcher.with_suffix(".route"), (1_791_000_009.25, 1_791_000_009.25))

    rc = _launch(monkeypatch, tmp_path, popen)

    out = capsys.readouterr().out
    assert rc == 0
    assert "launcher_at=1791000001500" in out.splitlines()
    assert "harness_at=1791000009250" in out.splitlines()


def test_a_route_report_removed_before_it_is_read_reads_as_no_route(tmp_path):
    assert init_agent._take_route_report(tmp_path / "gone.route") == ({}, None)


def test_terminal_that_never_starts_the_launcher_fails_and_discards_it(monkeypatch, tmp_path, capsys):
    rc = _launch(monkeypatch, tmp_path, lambda command, **kwargs: None)

    assert rc == 2
    assert "did not start the launcher within 0s" in capsys.readouterr().err
    assert not list((tmp_path / "runtime" / "agentihooks-claude-terminal").iterdir())


def test_macos_command_uses_terminal_app(monkeypatch, tmp_path):
    monkeypatch.setattr(init_agent.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: "/usr/bin/osascript")

    host, command = init_agent._launch_command(tmp_path / "launch.sh", tmp_path, "session", {})

    assert host == "macos"
    assert command[:2] == ["/usr/bin/osascript", "-e"]
    assert "Terminal" in command[2]


def test_native_linux_uses_first_supported_terminal(monkeypatch, tmp_path):
    monkeypatch.setattr(init_agent.platform, "system", lambda: "Linux")
    monkeypatch.setattr(init_agent, "_is_wsl", lambda environ: False)
    monkeypatch.setattr(
        init_agent.shutil,
        "which",
        lambda name: "/usr/bin/gnome-terminal" if name == "gnome-terminal" else None,
    )

    host, command = init_agent._launch_command(
        tmp_path / "launch.sh",
        tmp_path,
        "session",
        {"DISPLAY": ":0"},
    )

    assert host == "linux"
    assert command == ["/usr/bin/gnome-terminal", "--title", "session", "--", str(tmp_path / "launch.sh")]


def test_headless_linux_fails_clearly(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(init_agent.platform, "system", lambda: "Linux")
    monkeypatch.setattr(init_agent, "_is_wsl", lambda environ: False)

    rc = init_agent.main(
        ["--dir", str(tmp_path), "--dry-run"],
        {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "runtime")},
    )

    assert rc == 2
    assert "no DISPLAY or WAYLAND_DISPLAY" in capsys.readouterr().err


def test_packaged_skill_exists_and_routes_through_agentihooks():
    skill = Path(__file__).parents[1] / "profiles" / "package" / "skills" / "init-agent" / "SKILL.md"

    assert skill.is_file()
    text = skill.read_text()
    assert "name: init-agent" in text
    assert "agentihooks init-agent" in text


def _handoff(monkeypatch, tmp_path, popen, extra=(), env_extra=None):
    monkeypatch.setattr(
        init_agent.shutil, "which", lambda name: "/usr/bin/agentihooks" if name == "agentihooks" else None
    )
    monkeypatch.setattr(
        init_agent,
        "_launch_command",
        lambda launcher, directory, title, environ: ("linux", ["/usr/bin/terminal", str(launcher)]),
    )
    binding, profile_env = _profile(monkeypatch, tmp_path)
    original = {**profile_env, "AGENTIHOOKS_RUN_MODEL": "opus", "AGENTIHOOKS_RUN_EFFORT": "medium"}
    monkeypatch.setattr(binding, "process", lambda: (123, "claude", original, "alpha"))

    def validated_popen(command, **kwargs):
        popen(command, **kwargs)
        env = kwargs["env"]
        route = init_agent._read_route_report(Path(command[-1]).with_suffix(".route"))
        if route.get("status") == "routed":
            monkeypatch.setattr(binding, "process", lambda: (123, "claude", env, route["account"]))
            binding.validate(binding.inspect(Path(env["CLAUDE_CONFIG_DIR"]), "engineer", "claude")["canary"])

    monkeypatch.setattr(init_agent.subprocess, "Popen", validated_popen)
    env = {
        "HOME": str(tmp_path),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        "AH_CC_TOKEN_alpha": "tok",
        **profile_env,
    }
    env.update(env_extra or {})
    return init_agent.main(
        [
            "--dir",
            str(tmp_path),
            "--name",
            "handoff",
            "--prompt",
            "handoff document",
            "--handoff",
            "--start-timeout",
            "0.5",
            "--route-timeout",
            "0.5",
            *extra,
        ],
        env,
    )


def test_handoff_excludes_this_account_and_never_falls_back_to_bare(monkeypatch, tmp_path, capsys):
    rc = _handoff(monkeypatch, tmp_path, None, extra=["--dry-run"])

    assert rc == 0
    launcher = next((tmp_path / "runtime" / "agentihooks-claude-terminal").glob("*.sh"))
    text = launcher.read_text()
    assert "select-profile engineer --agent claude -- --agentihooks-exclude alpha --agentihooks-report" in text
    assert "--agentihooks-fallback-bare" not in text


def test_handoff_preserves_explicit_native_choices(monkeypatch, tmp_path, capsys):
    assert (
        _handoff(monkeypatch, tmp_path, None, extra=["--dry-run", "--", "--model", "chosen", "--effort", "high"]) == 0
    )
    out = capsys.readouterr().out
    assert "model=chosen\n" in out and "effort=high\n" in out
    launcher = next((tmp_path / "runtime" / "agentihooks-claude-terminal").glob("*.sh"))
    assert "--model chosen --effort high" in launcher.read_text()


def test_handoff_refuses_changed_effort_policy_before_terminal_launch(monkeypatch, tmp_path, capsys):
    assert (
        _handoff(
            monkeypatch,
            tmp_path,
            None,
            extra=["--dry-run"],
            env_extra={"AGENTIHOOKS_SWARM_LANE": "eng", "AGENTIHOOKS_SWARM_EFFORT_RANGE": "high:high"},
        )
        == 2
    )
    assert (
        capsys.readouterr().err
        == "agentihooks init-agent: unsupported quota transfer: saved effort is outside the current swarm range\n"
    )


def test_handoff_needs_a_handoff_document(monkeypatch, tmp_path, capsys):
    rc = init_agent.main(["--dir", str(tmp_path), "--handoff", "--dry-run"], {"HOME": str(tmp_path)})

    assert rc == 2
    assert "--handoff needs the handoff document" in capsys.readouterr().err


def _routed(status: str, account: str = ""):
    def popen(command, **kwargs):
        launcher = Path(command[-1])
        launcher.with_suffix(".started").touch()
        launcher.with_suffix(".route").write_text(f"status={status}\naccount={account}\nplacement=open\n")

    return popen


def test_handoff_marks_this_session_once_the_new_one_is_routed(monkeypatch, tmp_path, capsys):
    marked = []
    monkeypatch.setattr("hooks.context.account_sessions.agent_pid", lambda start=None: 4242)
    monkeypatch.setattr(
        "hooks.context.broadcast.mark_handed_off", lambda pid, account: marked.append((pid, account)) or ["sid-1"]
    )

    rc = _handoff(monkeypatch, tmp_path, _routed("routed", "beta"))

    out = capsys.readouterr().out
    assert rc == 0
    assert marked == [(4242, "beta")]
    assert "route_status=routed" in out
    assert "account=beta" in out
    assert "handoff=done" in out
    assert "handed_off_sessions=sid-1" in out


def test_failed_route_fails_the_handoff_and_marks_nothing(monkeypatch, tmp_path, capsys):
    marked = []
    monkeypatch.setattr("hooks.context.broadcast.mark_handed_off", lambda pid, account: marked.append(pid))

    rc = _handoff(monkeypatch, tmp_path, _routed("failed"))

    captured = capsys.readouterr()
    assert rc == 3
    assert marked == []
    assert "handoff=failed" in captured.out
    assert captured.err == "agentihooks init-agent: handoff failed; the new session was not routed to another account\n"


def test_agenti_writes_the_route_report_and_honours_exclusions(monkeypatch, tmp_path):
    from scripts import claude_quota_balancer as balancer
    from scripts import install

    alpha = balancer.parse_probe("alpha", "", 1)
    seen = {}

    def select(environ, **kwargs):
        seen.update(kwargs)
        credential = balancer.Credential("AH_CC_TOKEN_beta", "tok-b")
        return balancer.RouteDecision(credential, alpha, "cached", sessions=0, max_sessions=2)

    def execvpe(binary, cmd, env):
        raise SystemExit(0)

    report = tmp_path / "x.route"
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setattr(install.os, "environ", dict(install.os.environ))
    monkeypatch.setattr(install, "_load_claude_runtime_env", lambda: None)
    monkeypatch.setattr(balancer, "select_credential", select)
    monkeypatch.setattr("hooks.context.account_sessions.sessions_by_account", lambda: {"beta": 1})
    monkeypatch.setattr(install.os, "execvpe", execvpe)

    try:
        install.cmd_claude(["--agentihooks-exclude", "alpha", "--agentihooks-report", str(report), "--model", "opus"])
    except SystemExit:
        pass

    assert seen["exclude"] == ["alpha"]
    assert seen["sessions"] == {"beta": 1}
    assert report.read_text() == "status=routed\naccount=beta\nplacement=open\n"


def test_the_launcher_exports_the_agent_name_so_codex_can_be_found_by_it(tmp_path):
    launcher, _ = init_agent._write_launcher(
        tmp_path, "smoke codex", "", [], {"XDG_RUNTIME_DIR": str(tmp_path)}, init_agent.AgentSpec(agent="codex")
    )
    text = launcher.read_text()
    assert "export AGENTIHOOKS_AGENT_NAME='smoke codex'\n" in text
    assert text.index("AGENTIHOOKS_AGENT_NAME") < text.index("agentihooks codex ")


def test_the_shell_left_after_the_agent_exits_drops_the_agent_name(tmp_path):
    launcher, _ = init_agent._write_launcher(
        tmp_path, "smoke", "", [], {"XDG_RUNTIME_DIR": str(tmp_path), "SHELL": "/bin/bash"}, init_agent.AgentSpec()
    )
    text = launcher.read_text()
    assert text.index("unset AGENTIHOOKS_AGENT_NAME") < text.index("exec /bin/bash -l")


COLLECTOR = "http://10.10.30.130:4318"


def _launcher_text(tmp_path, environ, agent="claude", name="swarm-buildout-eng-4"):
    env = {"XDG_RUNTIME_DIR": str(tmp_path), **environ}
    launcher, _ = init_agent._write_launcher(tmp_path, name, "", [], env, init_agent.AgentSpec(agent=agent))
    return launcher.read_text()


def test_collector_unset_leaves_the_launcher_without_telemetry(tmp_path):
    for agent in ("claude", "codex"):
        text = _launcher_text(tmp_path, {}, agent)
        assert "OTEL" not in text
        assert "CLAUDE_CODE_ENABLE_TELEMETRY" not in text


def test_collector_set_exports_claude_native_telemetry(tmp_path):
    text = _launcher_text(tmp_path, {"AGENTIHOOKS_OTEL_COLLECTOR": COLLECTOR})
    for line in (
        "export CLAUDE_CODE_ENABLE_TELEMETRY=1\n",
        "export OTEL_METRICS_EXPORTER=otlp\n",
        "export OTEL_LOGS_EXPORTER=otlp\n",
        "export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf\n",
        f"export OTEL_EXPORTER_OTLP_ENDPOINT={COLLECTOR}\n",
    ):
        assert line in text
    assert text.index("OTEL_EXPORTER_OTLP_ENDPOINT") < text.index(" claude ")


def test_resource_attributes_carry_swarm_agent_lane_and_task(tmp_path):
    env = {
        "AGENTIHOOKS_OTEL_COLLECTOR": COLLECTOR,
        "AGENTIHOOKS_SWARM": "swarm-buildout",
        "AGENTIHOOKS_SWARM_LANE": "eng",
        "AGENTIHOOKS_SWARM_TASK": "t4",
    }
    text = _launcher_text(tmp_path, env)
    assert "export OTEL_RESOURCE_ATTRIBUTES=swarm=swarm-buildout,agent=swarm-buildout-eng-4,lane=eng,task=t4\n" in text


def test_resource_attributes_skip_absent_swarm_values(tmp_path):
    text = _launcher_text(tmp_path, {"AGENTIHOOKS_OTEL_COLLECTOR": COLLECTOR})
    assert "export OTEL_RESOURCE_ATTRIBUTES=agent=swarm-buildout-eng-4\n" in text


def test_a_swarm_spawn_exports_its_identity_and_enables_langfuse_traces(tmp_path):
    env = {"AGENTIHOOKS_SWARM": "swarm-buildout", "AGENTIHOOKS_SWARM_LANE": "eng", "AGENTIHOOKS_SWARM_TASK": "t14"}
    text = _launcher_text(tmp_path, env)
    for line in (
        "export AGENTIHOOKS_SWARM=swarm-buildout\n",
        "export AGENTIHOOKS_SWARM_LANE=eng\n",
        "export AGENTIHOOKS_SWARM_TASK=t14\n",
        "export AGENTIHOOKS_LANGFUSE_ENABLED=1\n",
    ):
        assert line in text
    assert text.index("AGENTIHOOKS_LANGFUSE_ENABLED") < text.index(" claude ")


def test_a_swarm_spawn_exports_its_compact_limit(tmp_path):
    env = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_COMPACT_LIMIT": "40"}
    text = _launcher_text(tmp_path, env)
    assert "export AGENTIHOOKS_COMPACT_LIMIT=40\n" in text
    assert text.index("AGENTIHOOKS_COMPACT_LIMIT") < text.index(" claude ")


def test_a_swarm_launcher_pins_its_own_pid(tmp_path):
    lines = _launcher_text(tmp_path, {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_SWARM_TASK": "t1"}).splitlines()
    assert {"export AGENTIHOOKS_SWARM=sw", "export AGENTIHOOKS_SWARM_TASK=t1"} <= set(lines)
    assert "export AGENTIHOOKS_SWARM_LAUNCHER=$$" in lines
    text = "\n".join(lines)
    assert text.index("AGENTIHOOKS_SWARM_LAUNCHER") < text.index(" claude ")


AGENT_ENV = {
    "AGENTIHOOKS_AGENT_NAME": "engineer@abcdef-0001",
    "AGENTIHOOKS_SWARM": "sw",
    "AGENTIHOOKS_SWARM_LANE": "eng",
    "AGENTIHOOKS_SWARM_TASK": "t1",
    "AGENTIHOOKS_SWARM_LAUNCHER": "10",
}


def _dry_launcher(monkeypatch, tmp_path, name, environ, *extra):
    monkeypatch.setattr(
        init_agent,
        "_launch_command",
        lambda launcher, directory, title, env: ("linux", ["/usr/bin/terminal", str(launcher)]),
    )
    runtime = tmp_path / "runtime"
    env = {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(runtime), **environ}
    argv = ["--dir", str(tmp_path), "--name", name, "--agent", "claude", *extra, "--dry-run"]
    assert init_agent.main(argv, env) == 0
    return next((runtime / "agentihooks-claude-terminal").glob("*.sh")).read_text()


def test_a_launch_from_an_agent_starts_without_its_swarm_identity(monkeypatch, tmp_path):
    text = _dry_launcher(monkeypatch, tmp_path, "proof", AGENT_ENV)
    assert "AGENTIHOOKS_SWARM" not in text
    assert "export AGENTIHOOKS_AGENT_NAME=proof\n" in text


def test_a_tick_spawn_keeps_its_swarm_identity_without_the_mark(monkeypatch, tmp_path):
    text = _dry_launcher(monkeypatch, tmp_path, "engineer@abcdef-0002", {**AGENT_ENV, "AGENTIHOOKS_SWARM_SPAWN": "1"})
    assert "export AGENTIHOOKS_SWARM_TASK=t1\n" in text
    assert "export AGENTIHOOKS_SWARM_LAUNCHER=$$\n" in text
    assert "AGENTIHOOKS_SWARM_SPAWN" not in text


def test_a_quota_handoff_keeps_its_swarm_identity(monkeypatch, tmp_path):
    assert _handoff(monkeypatch, tmp_path, None, extra=["--dry-run"], env_extra=AGENT_ENV) == 0
    launcher = next((tmp_path / "runtime" / "agentihooks-claude-terminal").glob("*.sh"))
    assert "export AGENTIHOOKS_SWARM_TASK=t1\n" in launcher.read_text()


def test_an_agent_relaunching_itself_keeps_its_swarm_identity(monkeypatch, tmp_path):
    text = _dry_launcher(monkeypatch, tmp_path, "engineer@abcdef-0001", AGENT_ENV)
    assert "export AGENTIHOOKS_SWARM_TASK=t1\n" in text


def test_a_quota_handoff_names_this_session_as_the_predecessor(monkeypatch, tmp_path):
    env = {"CLAUDE_CODE_SESSION_ID": "sess-old", "AGENTIHOOKS_PREDECESSOR_SESSION": "sess-older"}
    assert _handoff(monkeypatch, tmp_path, None, extra=["--dry-run"], env_extra=env) == 0
    launcher = next((tmp_path / "runtime" / "agentihooks-claude-terminal").glob("*.sh")).read_text()
    assert "export AGENTIHOOKS_PREDECESSOR_SESSION=sess-old\n" in launcher
    assert launcher.index("AGENTIHOOKS_PREDECESSOR_SESSION") < launcher.index(" claude ")


def test_a_quota_handoff_without_a_session_id_names_no_predecessor(monkeypatch, tmp_path):
    env = {"AGENTIHOOKS_PREDECESSOR_SESSION": "sess-older"}
    assert _handoff(monkeypatch, tmp_path, None, extra=["--dry-run"], env_extra=env) == 0
    launcher = next((tmp_path / "runtime" / "agentihooks-claude-terminal").glob("*.sh")).read_text()
    assert "unset AGENTIHOOKS_PREDECESSOR_SESSION\n" in launcher
    assert "export AGENTIHOOKS_PREDECESSOR_SESSION" not in launcher


def test_a_tick_spawn_keeps_the_predecessor_the_tick_named(monkeypatch, tmp_path):
    env = {**AGENT_ENV, "AGENTIHOOKS_SWARM_SPAWN": "1", "AGENTIHOOKS_PREDECESSOR_SESSION": "sess-old"}
    text = _dry_launcher(monkeypatch, tmp_path, "engineer@abcdef-0002", {**env, "CLAUDE_CODE_SESSION_ID": "sess-x"})
    assert "export AGENTIHOOKS_PREDECESSOR_SESSION=sess-old\n" in text


@pytest.mark.parametrize("spawn", [{}, {"AGENTIHOOKS_SWARM_SPAWN": "1"}])
def test_a_launch_that_is_no_transfer_clears_any_inherited_predecessor(monkeypatch, tmp_path, spawn):
    env = {**AGENT_ENV, "CLAUDE_CODE_SESSION_ID": "sess-x", **spawn}
    inherited = {} if spawn else {"AGENTIHOOKS_PREDECESSOR_SESSION": "sess-old"}
    text = _dry_launcher(monkeypatch, tmp_path, "proof", {**env, **inherited})
    assert "unset AGENTIHOOKS_PREDECESSOR_SESSION\n" in text
    assert "export AGENTIHOOKS_PREDECESSOR_SESSION" not in text


def test_the_launch_environment_drops_identity_and_keeps_swarm_settings():
    environ = {
        **AGENT_ENV,
        "AGENTIHOOKS_SWARM_AUTONOMY": "full",
        "AGENTIHOOKS_SWARM_EFFORT_RANGE": "low:high",
        "AGENTIHOOKS_SWARM_REDIS_URL": "redis://r",
    }
    dropped = init_agent._launch_environ(environ, "proof", False)
    assert dropped == {"AGENTIHOOKS_AGENT_NAME": "engineer@abcdef-0001", "AGENTIHOOKS_SWARM_REDIS_URL": "redis://r"}
    assert init_agent._launch_environ(environ, "proof", True) == environ
    assert init_agent._launch_environ({**environ, "AGENTIHOOKS_SWARM_SPAWN": "1"}, "proof", False) == environ


def test_a_launch_outside_a_swarm_leaves_langfuse_alone(tmp_path):
    text = _launcher_text(tmp_path, {})
    assert "AGENTIHOOKS_SWARM" not in text
    assert "LANGFUSE" not in text


def test_codex_gets_an_otel_exporter_override(tmp_path):
    text = _launcher_text(tmp_path, {"AGENTIHOOKS_OTEL_COLLECTOR": COLLECTOR}, "codex")
    assert "otel.exporter=" in text
    assert f"{COLLECTOR}/v1/logs" in text
    assert text.index("otel.exporter=") > text.index("codex")


def _command_line(tmp_path, environ, agent, agent_args=()):
    env = {"XDG_RUNTIME_DIR": str(tmp_path), **environ}
    launcher, _ = init_agent._write_launcher(
        tmp_path, "m", "", list(agent_args), env, init_agent.AgentSpec(agent=agent)
    )
    return next(line for line in launcher.read_text().splitlines() if " --name m" in line or "codex " in line)


def test_claude_defaults_to_opus_at_high_effort(tmp_path):
    assert "--model opus --effort high" in _command_line(tmp_path, {}, "claude")


def test_codex_defaults_to_sol_at_high_effort(tmp_path):
    line = _command_line(tmp_path, {}, "codex")
    assert "-m gpt-6.1-sol" in line and 'model_reasoning_effort="high"' in line


def _resume_line(tmp_path, agent):
    launcher, _ = init_agent._write_launcher(
        tmp_path, "m", "", [], {"XDG_RUNTIME_DIR": str(tmp_path)}, init_agent.AgentSpec(agent=agent, resume="c0ffee")
    )
    return next(line for line in launcher.read_text().splitlines() if " --name m" in line or "codex " in line)


def test_a_claude_resume_reopens_that_conversation(tmp_path):
    assert "--name m --resume c0ffee" in _resume_line(tmp_path, "claude")


def test_a_codex_resume_runs_the_resume_subcommand_before_any_flag(tmp_path):
    words = _resume_line(tmp_path, "codex").split()
    at = words.index("resume")
    assert words[at + 1] == "c0ffee"
    assert words[at - 1] != "--agentihooks-report" and words.index("-m") > at


def test_init_agent_passes_resume_through_to_the_launch(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        init_agent.shutil, "which", lambda name: "/usr/bin/agentihooks" if name == "agentihooks" else None
    )
    monkeypatch.setattr(
        init_agent, "_launch_command", lambda launcher, directory, title, environ: ("linux", ["/usr/bin/terminal"])
    )
    _, profile_env = _profile(monkeypatch, tmp_path, "codex")
    env = {"XDG_RUNTIME_DIR": str(tmp_path), **profile_env}
    args = ["--dir", str(tmp_path), "--name", "m", "--agent", "codex", "--resume", "c0ffee", "--dry-run"]
    assert init_agent.main(args, env) == 0
    launcher = next(
        line.split("=", 1)[1] for line in capsys.readouterr().out.splitlines() if line.startswith("launcher=")
    )
    assert "resume c0ffee" in Path(launcher).read_text()


def test_model_and_effort_come_from_env_per_agent(tmp_path):
    env = {
        "AGENTIHOOKS_CLAUDE_MODEL": "fable",
        "AGENTIHOOKS_CLAUDE_EFFORT": "max",
        "AGENTIHOOKS_CODEX_MODEL": "gpt-x",
        "AGENTIHOOKS_CODEX_EFFORT": "xhigh",
    }
    assert "--model fable --effort max" in _command_line(tmp_path, env, "claude")
    codex = _command_line(tmp_path, env, "codex")
    assert "-m gpt-x" in codex and 'model_reasoning_effort="xhigh"' in codex


def test_explicit_model_and_effort_flags_win_over_defaults(tmp_path):
    line = _command_line(tmp_path, {}, "claude", ["--model", "sonnet", "--effort", "low"])
    assert "opus" not in line and "--effort high" not in line
    assert "--model sonnet --effort low" in line


def test_model_effort_names_what_the_launch_uses(tmp_path):
    assert init_agent.model_effort("claude", [], {}) == ("opus", "high")
    assert init_agent.model_effort("codex", [], {"AGENTIHOOKS_CODEX_EFFORT": "xhigh"}) == ("gpt-6.1-sol", "xhigh")
    assert init_agent.model_effort("claude", ["--model", "sonnet", "--effort=low"], {}) == ("sonnet", "low")
    assert init_agent.model_effort("codex", ["-m", "o3", "-c", 'model_reasoning_effort="low"'], {}) == ("o3", "low")


def test_report_names_the_model_and_effort(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        init_agent,
        "_launch_command",
        lambda launcher, directory, title, environ: ("linux", ["/usr/bin/terminal", str(launcher)]),
    )
    rc = init_agent.main(
        ["--dir", str(tmp_path), "--dry-run", "--", "--model", "fable"],
        {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "rt"), "SHELL": "/bin/bash"},
    )
    out = capsys.readouterr().out.splitlines()
    assert rc == 0 and "model=fable" in out and "effort=high" in out


def _trust_launch(monkeypatch, tmp_path, project, env_extra=None):
    seen = {}

    def popen(command, **kwargs):
        config = tmp_path / ".claude.json"
        seen["config"] = config.read_text() if config.exists() else ""
        Path(command[-1]).with_suffix(".started").touch()

    monkeypatch.setattr(init_agent.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        init_agent,
        "_launch_command",
        lambda launcher, directory, title, environ: ("linux", ["/usr/bin/terminal", str(launcher)]),
    )
    monkeypatch.setattr(init_agent.subprocess, "Popen", popen)
    rc = init_agent.main(
        ["--dir", str(project), "--name", "trust", "--agent", "claude", "--start-timeout", "0", "--route-timeout", "0"],
        {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "runtime"), **(env_extra or {})},
    )
    return rc, seen


def test_launching_into_an_untrusted_folder_marks_it_trusted_before_the_session_starts(monkeypatch, tmp_path, capsys):
    project = tmp_path / "fresh"
    project.mkdir()
    (tmp_path / ".claude.json").write_text(json.dumps({"numStartups": 3, "projects": {"/elsewhere": {"x": 1}}}))

    rc, seen = _trust_launch(monkeypatch, tmp_path, project)

    assert rc == 0
    assert "trust=marked" in capsys.readouterr().out
    before_start = json.loads(seen["config"])
    assert before_start["projects"][str(project)]["hasTrustDialogAccepted"] is True
    assert before_start["projects"]["/elsewhere"] == {"x": 1}
    assert before_start["numStartups"] == 3


def test_a_folder_already_trusted_is_left_as_is(monkeypatch, tmp_path, capsys):
    project = tmp_path / "repo" / "worktree"
    project.mkdir(parents=True)
    config = tmp_path / ".claude.json"
    original = json.dumps({"projects": {str(tmp_path / "repo"): {"hasTrustDialogAccepted": True}}})
    config.write_text(original)

    rc, _ = _trust_launch(monkeypatch, tmp_path, project)

    assert rc == 0
    assert "trust=trusted" in capsys.readouterr().out
    assert config.read_text() == original


@pytest.mark.parametrize(
    ("config_text", "env_extra"),
    [("{not json", {}), ("{}", {"AGENTIHOOKS_TRUST_LAUNCH_DIR": "0"})],
    ids=["unreadable-config", "setting-off"],
)
def test_when_trust_cannot_be_set_the_caller_is_told(monkeypatch, tmp_path, capsys, config_text, env_extra):
    project = tmp_path / "fresh"
    project.mkdir()
    config = tmp_path / ".claude.json"
    config.write_text(config_text)

    rc, _ = _trust_launch(monkeypatch, tmp_path, project, env_extra)

    captured = capsys.readouterr()
    assert rc == 0
    assert "trust=untrusted" in captured.out
    assert "folder trust question" in captured.err
    assert config.read_text() == config_text


@pytest.mark.parametrize(
    ("caller_home", "pane_home"),
    [("profile", None), (None, "herdr")],
    ids=["caller-profile-home", "pane-foreign-home"],
)
def test_the_launched_claude_reads_the_config_holding_the_recorded_trust(monkeypatch, tmp_path, caller_home, pane_home):
    project = tmp_path / "fresh"
    project.mkdir()
    seen = tmp_path / "seen"
    stub = tmp_path / "agentihooks"
    stub.write_text(f'#!/bin/sh\nprintf %s "${{CLAUDE_CONFIG_DIR:-}}" > {seen}\n')
    stub.chmod(0o755)
    real_popen = subprocess.Popen

    def popen(command, **kwargs):
        if command[0] != "/usr/bin/terminal":
            return real_popen(command, **kwargs)
        pane = {"HOME": str(tmp_path), "PATH": os.environ["PATH"]}
        if pane_home:
            pane["CLAUDE_CONFIG_DIR"] = str(tmp_path / pane_home)
        real_popen(["bash", command[-1]], env=pane).wait()

    monkeypatch.setattr(init_agent.shutil, "which", lambda name: str(stub) if name == "agentihooks" else None)
    monkeypatch.setattr(
        init_agent,
        "_launch_command",
        lambda launcher, directory, title, environ: ("linux", ["/usr/bin/terminal", str(launcher)]),
    )
    monkeypatch.setattr(init_agent.subprocess, "Popen", popen)
    caller = {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "runtime"), "SHELL": "/bin/true"}
    if caller_home:
        (tmp_path / caller_home).mkdir()
        caller["CLAUDE_CONFIG_DIR"] = str(tmp_path / caller_home)

    rc = init_agent.main(
        ["--dir", str(project), "--name", "trust", "--agent", "claude", "--start-timeout", "5", "--route-timeout", "0"],
        caller,
    )

    assert rc == 0
    launched = {"HOME": str(tmp_path), **({"CLAUDE_CONFIG_DIR": seen.read_text()} if seen.read_text() else {})}
    config = json.loads(claude_json(launched).read_text())
    assert config["projects"][str(project)]["hasTrustDialogAccepted"] is True


def test_a_codex_launcher_leaves_the_claude_config_home_alone(tmp_path):
    text = _launcher_text(tmp_path, {"CLAUDE_CONFIG_DIR": str(tmp_path / "profile")}, "codex")
    assert "CLAUDE_CONFIG_DIR" not in text
    lines = text.splitlines()
    assert lines[lines.index("export AGENTIHOOKS_AGENT_NAME=swarm-buildout-eng-4") + 1] == "sleep 3"
    after_name = lines[lines.index("export AGENTIHOOKS_AGENT_NAME=swarm-buildout-eng-4") + 2]
    assert after_name.startswith("/") and " codex " in after_name


def test_codex_trusts_exactly_its_launch_folder_for_that_session(tmp_path):
    project = tmp_path / "repo.with.dots"
    project.mkdir()
    env = {"XDG_RUNTIME_DIR": str(tmp_path)}
    launcher, _ = init_agent._write_launcher(project, "m", "", [], env, init_agent.AgentSpec(agent="codex"))
    words = next(line for line in launcher.read_text().splitlines() if "codex " in line)
    import shlex

    argv = shlex.split(words)
    assert argv[argv.index(f'projects={{"{project}"={{trust_level="trusted"}}}}') - 1] == "-c"


def test_codex_launch_trust_follows_the_trust_setting(tmp_path):
    env = {"XDG_RUNTIME_DIR": str(tmp_path), "AGENTIHOOKS_TRUST_LAUNCH_DIR": "0"}
    launcher, _ = init_agent._write_launcher(tmp_path, "m", "", [], env, init_agent.AgentSpec(agent="codex"))
    assert "trust_level" not in launcher.read_text()


def test_a_claude_launch_gets_no_codex_trust_override(tmp_path):
    launcher, _ = init_agent._write_launcher(
        tmp_path, "m", "", [], {"XDG_RUNTIME_DIR": str(tmp_path)}, init_agent.AgentSpec(agent="claude")
    )
    assert "trust_level" not in launcher.read_text()


@pytest.mark.parametrize(("agent", "named"), [("claude", True), ("codex", False)])
def test_inbox_channel_puts_the_channel_flags_first_for_claude_only(monkeypatch, tmp_path, capsys, agent, named):
    from scripts.inbox import channel

    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda requested, environ: (agent, "requested"))
    monkeypatch.setattr(init_agent, "_launch_command", lambda launcher, directory, title, environ: ("linux", ["t"]))
    argv = ["--dir", str(tmp_path), "--agent", agent, "--inbox-channel", "--dry-run", "--", "--model", "opus"]
    assert init_agent.main(argv, {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "rt")}) == 0
    line = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("claude_args="))
    assert line.startswith("claude_args='--mcp-config=") is named and (channel.FLAG in line) is named
    assert line.endswith("--model opus")


@pytest.mark.parametrize("channel", [True, False])
def test_a_channel_launch_pins_legacy_mcp_negotiation_before_claude(tmp_path, channel):
    env = {"XDG_RUNTIME_DIR": str(tmp_path)}
    spec = init_agent.AgentSpec(channel=channel)
    launcher, _ = init_agent._write_launcher(tmp_path, "eng", "", [], env, spec)
    text = launcher.read_text()
    assert ("export MCP_PROTOCOL_NEGOTIATION=legacy" in text.splitlines()) is channel
    assert not channel or text.index("MCP_PROTOCOL_NEGOTIATION") < text.index(" claude ")


def test_the_inbox_channel_flag_explains_itself():
    found = next(a for a in init_agent._parser()._actions if a.dest == "inbox_channel")
    assert (
        found.help == "Claude: load the agentihooks inbox channel and answer its development channels warning in herdr"
    )


@pytest.mark.parametrize(("seen", "said"), [(True, "answered"), (False, "unseen")])
def test_the_channel_warning_is_answered_in_the_agents_pane(monkeypatch, seen, said):
    from scripts.inbox import channel

    calls = []
    monkeypatch.setattr(init_agent.herdr_host, "answer", lambda *args: calls.append(args) or seen)
    assert init_agent._answer_channel_warning("w1:p2", {"K": "v"}) == said
    assert calls == [("w1:p2", channel.WARNING, {"K": "v"}, init_agent.CHANNEL_WARNING_MS)]


def test_a_channel_dry_run_writes_the_negotiation_pin_into_its_launcher(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda requested, environ: ("claude", "requested"))
    monkeypatch.setattr(init_agent, "_launch_command", lambda launcher, directory, title, environ: ("linux", ["t"]))
    argv = ["--dir", str(tmp_path), "--agent", "claude", "--inbox-channel", "--dry-run"]
    assert init_agent.main(argv, {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "rt")}) == 0
    launcher = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("launcher="))
    lines = Path(launcher.split("=", 1)[1]).read_text().splitlines()
    assert "export MCP_PROTOCOL_NEGOTIATION=legacy" in lines


def test_a_hand_launch_with_no_account_launches_bare_claude_with_its_channel(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda requested, environ: ("", "every account is full"))
    monkeypatch.setattr(init_agent, "_launch_command", lambda launcher, directory, title, environ: ("linux", ["t"]))
    argv = ["--dir", str(tmp_path), "--inbox-channel", "--dry-run"]
    assert init_agent.main(argv, {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "rt")}) == 0
    out = capsys.readouterr().out.splitlines()
    assert "agent=claude" in out
    launcher = next(x for x in out if x.startswith("launcher="))
    lines = Path(launcher.split("=", 1)[1]).read_text().splitlines()
    assert "export MCP_PROTOCOL_NEGOTIATION=legacy" in lines


def _codex_hooks(home, first):
    ours = {"hooks": [{"type": "command", "command": str(home / "agentihooks-hook.sh")}]}
    herdr = {"hooks": [{"command": "bash herdr-agent-state.sh session", "timeout": 10, "type": "command"}]}
    home.mkdir(parents=True, exist_ok=True)
    groups = [ours, herdr] if first == "ours" else [herdr, ours]
    (home / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": groups, "Stop": groups}}, indent=2))
    return home / "hooks.json", ours


@pytest.mark.parametrize(
    ("first", "field"), [("herdr", "codex_hooks=restored:SessionStart,Stop"), ("ours", "codex_hooks=unchanged")]
)
def test_a_codex_launch_restores_the_approved_hook_order_before_codex_starts(
    monkeypatch, tmp_path, capsys, first, field
):
    path, ours = _codex_hooks(tmp_path / ".codex", first)
    untouched = path.stat().st_mtime_ns
    seen = {}

    def popen(command, **kwargs):
        seen["first"] = json.loads(path.read_text())["hooks"]["SessionStart"][0]
        Path(command[-1]).with_suffix(".started").touch()

    monkeypatch.setattr(init_agent.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        init_agent, "_launch_command", lambda launcher, directory, title, environ: ("linux", ["t", str(launcher)])
    )
    monkeypatch.setattr(init_agent.subprocess, "Popen", popen)
    argv = ["--dir", str(tmp_path), "--name", "cx", "--agent", "codex", "--start-timeout", "0", "--route-timeout", "0"]

    assert init_agent.main(argv, {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "rt")}) == 0
    assert field in capsys.readouterr().out.splitlines()
    assert seen["first"] == ours
    assert first == "herdr" or path.stat().st_mtime_ns == untouched


@pytest.mark.parametrize("source,target", [(None, "claude"), ("claude", "claude"), ("codex", "codex")])
@pytest.mark.parametrize("route", [["--route", "api"], ["--route=api"]])
@pytest.mark.parametrize("explicit", [False, True])
def test_api_handoff_launches_the_requested_harness(monkeypatch, tmp_path, capsys, source, target, route, explicit):
    binding, profile_env = _profile(monkeypatch, tmp_path, target)
    original = {
        **profile_env,
        "AGENTIHOOKS_RUN_MODEL": "opus" if target == "claude" else "gpt-6.1-sol",
        "AGENTIHOOKS_RUN_EFFORT": "medium",
    }
    monkeypatch.setattr(binding, "process", lambda: (123, target, original, "alpha"))
    monkeypatch.setattr(init_agent, "_launch_command", lambda *args: ("linux", ["terminal"]))
    monkeypatch.setattr(init_agent.operator_env, "fill", lambda env: None)
    result = init_agent.main(
        [
            "--dir",
            str(tmp_path),
            "--name",
            "api-handoff",
            *(["--agent", target] if explicit else []),
            "--prompt",
            "saved task",
            "--handoff",
            "--dry-run",
            "--",
            *route,
        ],
        {
            "HOME": str(tmp_path),
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
            **({"AGENTIHOOKS_TARGET": source} if source else {}),
            **profile_env,
        },
    )
    assert result == 0
    report = capsys.readouterr().out
    assert f"agent={target}\n" in report
    assert "agent_reason=handoff\n" in report
    launcher = next(line.split("=", 1)[1] for line in report.splitlines() if line.startswith("launcher="))
    command = Path(launcher).read_text()
    assert f" {target} " in command
    assert "--route" in command
    assert "api" in command


@pytest.mark.parametrize("extra", [[], ["--", "--route", "token"], ["--", "--route-timeout", "api"]])
def test_codex_subscription_handoff_remains_unsupported(monkeypatch, tmp_path, capsys, extra):
    monkeypatch.setattr(init_agent.operator_env, "fill", lambda env: None)
    assert (
        init_agent.main(
            ["--dir", str(tmp_path), "--handoff", "--prompt", "saved task", "--dry-run", *extra],
            {"HOME": str(tmp_path), "AGENTIHOOKS_TARGET": "codex"},
        )
        == 2
    )
    assert "unsupported quota transfer" in capsys.readouterr().err
