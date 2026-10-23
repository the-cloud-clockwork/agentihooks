import calendar
import json
import os
import shlex
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
        f"/usr/bin/agentihooks select-profile engineer --home={profile_env['CLAUDE_CONFIG_DIR']} --agent claude -- "
        f"--agentihooks-fallback-bare --agentihooks-report {route_report} --name quota-test"
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


def test_a_session_launch_starts_the_supervised_scheduler(monkeypatch, tmp_path):
    from scripts.scheduler import service

    calls = []
    monkeypatch.setattr(service, "ensure_running", lambda environ: calls.append(environ))

    def popen(command, **kwargs):
        Path(command[-1]).with_suffix(".started").touch()

    assert _launch(monkeypatch, tmp_path, popen) == 0
    assert len(calls) == 1
    assert calls[0]["HOME"] == str(tmp_path)


def test_a_dry_launch_does_not_start_the_scheduler(monkeypatch, tmp_path):
    from scripts.scheduler import service

    monkeypatch.setattr(service, "ensure_running", lambda environ: pytest.fail("dry launch started scheduler"))
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda requested, environ: ("claude", "rotation"))
    monkeypatch.setattr(init_agent, "_launch_command", lambda *args: ("linux", ["terminal"]))
    assert init_agent.main(["--dir", str(tmp_path), "--dry-run"], {"HOME": str(tmp_path)}) == 0


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


def test_a_profiled_launch_with_no_route_report_fails_at_the_route_timeout(tmp_path):
    from scripts.profiles import binding

    report = tmp_path / "report.json"
    binding.request(report, "engineer", "codex")
    env = {binding.REPORT: str(report)}
    with pytest.raises(ValueError) as error:
        init_agent._route_reported(env, 90.0, {})
    assert str(error.value) == "the harness did not report its route within 90s"
    with pytest.raises(ValueError) as empty:
        init_agent._route_reported(env, 0.5, {"status": ""})
    assert str(empty.value) == "the harness did not report its route within 0.5s"
    with pytest.raises(ValueError) as failed:
        init_agent._route_reported(env, 90.0, {"status": "failed", "error": "no Codex account has room"})
    assert str(failed.value) == "the harness route is failed: no Codex account has room"
    with pytest.raises(ValueError) as silent:
        init_agent._route_reported(env, 90.0, {"status": "failed"})
    assert str(silent.value) == "the harness route is failed: no error given"
    for started in ("routed", "bare", "direct"):
        assert init_agent._route_reported(env, 90.0, {"status": started}) is None
    assert init_agent._route_reported({}, 90.0, {}) is None
    assert init_agent._route_reported({binding.REPORT: ""}, 90.0, {}) is None
    binding.refuse(report, "selection refused")
    assert init_agent._route_reported(env, 90.0, {}) is None


def test_a_codex_launch_that_never_reports_its_route_fails_without_the_binding_wait(monkeypatch, tmp_path, capsys):
    from scripts.swarm.runtime import parse_fields

    binding, _ = _profile(monkeypatch, tmp_path, "codex")
    waited = []
    monkeypatch.setattr(binding, "wait", lambda *args: waited.append(args))
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda *a: ("codex", "explicit"))

    def launch(launcher, directory, name, args, agent, environ):
        init_agent._started_marker(launcher).touch()
        assert Path(environ[binding.REPORT]).is_file()
        return []

    monkeypatch.setattr(init_agent, "_start_herdr", launch)
    argv = ["--host", "herdr", "--agent", "codex", "--profile", "engineer", "--dir", str(tmp_path)]
    rc = init_agent.main([*argv, "--route-timeout", "0.1"], {"XDG_RUNTIME_DIR": str(tmp_path / "runtime")})
    output = capsys.readouterr()
    fields = parse_fields(output.out)
    assert rc == 3
    assert waited == []
    assert (fields["status"], fields["route_status"], fields["profile_validation"]) == ("started", "pending", "failed")
    assert output.err == "agentihooks init-agent: the harness did not report its route within 0.1s\n"


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
    home = tmp_path / "engineer" / "claude"
    assert (
        f"select-profile engineer --home={home} --agent claude -- --agentihooks-exclude alpha --agentihooks-report"
        in text
    )
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


def test_a_master_handoff_launches_at_the_nearest_effort_inside_the_swarm_range(monkeypatch, tmp_path, capsys):
    env = {"AGENTIHOOKS_SWARM_LANE": "master", "AGENTIHOOKS_SWARM_EFFORT_RANGE": "high:high"}
    assert _handoff(monkeypatch, tmp_path, None, extra=["--dry-run"], env_extra=env) == 0
    assert "effort=high\n" in capsys.readouterr().out
    launcher = next((tmp_path / "runtime" / "agentihooks-claude-terminal").glob("*.sh"))
    assert "--model opus --effort high" in launcher.read_text()


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


@pytest.mark.parametrize(("extra", "recorded"), [(("--resume", "sid-9"), ["handed-off-4242-sid-9"]), ((), [])])
def test_a_resumed_handoff_records_the_process_it_leaves(monkeypatch, tmp_path, extra, recorded):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("hooks.lifecycle.handoff_close._home", lambda: home)
    monkeypatch.setattr("hooks.context.account_sessions.agent_pid", lambda start=None: 4242)
    monkeypatch.setattr("hooks.context.broadcast.mark_handed_off", lambda pid, account: [])

    assert _handoff(monkeypatch, tmp_path, _routed("routed", "beta"), extra) == 0
    assert sorted(path.name for path in home.iterdir()) == recorded


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


def test_hive_launch_refreshes_github_credentials_by_reference(tmp_path):
    launcher = _launcher_text(tmp_path, {"HIVE_GITHUB_AUTH_MODE": "app", "GITHUB_APP_PRIVATE_KEY": "fixture"})
    assert "scripts.github_app_cli exec" in launcher
    assert "fixture" not in launcher


def test_workstation_launch_without_hive_auth_keeps_its_existing_github_environment(tmp_path):
    assert "scripts.github_app_cli" not in _launcher_text(tmp_path, {})


def test_swarm_display_name_preserves_the_inbox_identity(monkeypatch, tmp_path):
    text = _dry_launcher(
        monkeypatch,
        tmp_path,
        "engineer-a1b2c3-0001",
        {
            "AGENTIHOOKS_SWARM": "sw",
            "AGENTIHOOKS_SWARM_SPAWN": "1",
            "AGENTIHOOKS_AGENT_NAME": "engineer@a1b2c3-0001",
        },
    )
    assert "export AGENTIHOOKS_AGENT_NAME=engineer@a1b2c3-0001" in text
    assert "--name engineer-a1b2c3-0001" in text


def test_an_independent_launch_does_not_inherit_another_inbox_identity(tmp_path):
    text = _launcher_text(tmp_path, {"AGENTIHOOKS_AGENT_NAME": "engineer@a1b2c3-0001"}, name="my-session")
    assert "export AGENTIHOOKS_AGENT_NAME=my-session" in text


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


def test_hive_slot_markers_survive_a_launcher_without_the_callers_environment(tmp_path):
    import subprocess

    env = {"AGENTIHOOKS_SWARM": "rig", "AGENTIHOOKS_SLOT_HIVE": "hive-a", "AGENTIHOOKS_SLOT_EXECUTION": "execution-a"}
    text = _launcher_text(tmp_path, env)
    assert "export AGENTIHOOKS_SLOT_HIVE=hive-a\n" in text
    assert "export AGENTIHOOKS_SLOT_EXECUTION=execution-a\n" in text
    script = init_agent._swarm_exports(env) + 'printf "%s\\n%s" "$AGENTIHOOKS_SLOT_HIVE" "$AGENTIHOOKS_SLOT_EXECUTION"'
    result = subprocess.run(["bash", "-c", script], env={}, capture_output=True, text=True, check=True)
    assert result.stdout == "hive-a\nexecution-a"


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


def test_a_swarm_launcher_exports_the_agent_s_ledger_token_and_drops_the_server_secret(tmp_path):
    (tmp_path / ".env").write_text("PLAIN=1\n")
    env = {"HOME": str(tmp_path), "AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_LEDGER_AGENT_TOKEN": "derived-one"}
    text = _launcher_text(tmp_path, env)
    lines = text.splitlines()
    assert "export AGENTIHOOKS_LEDGER_AGENT_TOKEN=derived-one" in lines
    assert "unset SWARM_SECRET" in lines
    assert text.index("unset SWARM_SECRET") < text.index(" claude ")
    assert text.index(init_agent.operator_env.SOURCE) < text.index("unset SWARM_SECRET")
    assert "AGENTIHOOKS_LEDGER_AGENT_TOKEN" not in _launcher_text(tmp_path, {"AGENTIHOOKS_SWARM": "sw"})


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


def test_a_claimed_launch_exports_the_pinned_claim_from_its_option(monkeypatch, tmp_path):
    fence = json.dumps({"key": "claim-a", "revision": 7})
    text = _dry_launcher(
        monkeypatch,
        tmp_path,
        "engineer@abcdef-0002",
        {**AGENT_ENV, "AGENTIHOOKS_SWARM_SPAWN": "1"},
        "--claim-fence",
        fence,
    )
    assert "export AGENTIHOOKS_CLAIM_FENCE=" in text
    assert fence in text


def test_the_claim_fence_option_documents_its_identity_and_defaults_to_no_claim():
    parser = init_agent._parser()
    assert parser.parse_args([]).claim_fence == ""
    action = next(item for item in parser._actions if item.dest == "claim_fence")
    assert action.help == "Pinned event bus claim identity for this execution"


@pytest.mark.parametrize("name", ["engineer@abcdef-0002", "master@abcdef-0001"])
def test_swarm_launch_replaces_only_the_inherited_gateway_key(monkeypatch, tmp_path, capsys, name):
    state = tmp_path / ".agentihooks"
    state.mkdir()
    managed = "managed-gateway"
    inherited = "inherited-gateway"
    other = "inherited-other"
    (state / ".env").write_text(f"MCP_KEY_GATEWAY={managed}\nOTHER_KEY=file-other\n")
    observed = {}

    def terminal(launcher, directory, title, environ):
        observed.update(environ)
        return "linux", ["/usr/bin/terminal", str(launcher)]

    monkeypatch.setattr(init_agent, "_launch_command", terminal)
    assert (
        init_agent.main(
            ["--dir", str(tmp_path), "--name", name, "--agent", "claude", "--dry-run"],
            {
                "HOME": str(tmp_path),
                "PATH": os.environ["PATH"],
                "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
                "AGENTIHOOKS_SWARM": "example",
                "AGENTIHOOKS_SWARM_SPAWN": "1",
                "MCP_KEY_GATEWAY": inherited,
                "OTHER_KEY": other,
            },
        )
        == 0
    )
    assert observed["MCP_KEY_GATEWAY"] == managed
    assert observed["OTHER_KEY"] == other
    output = capsys.readouterr()
    assert all(value not in output.out + output.err for value in (managed, inherited, other))
    launcher = next((tmp_path / "runtime" / "agentihooks-claude-terminal").glob("*.sh")).read_text()
    assert all(value not in launcher for value in (managed, inherited, other))


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


def test_claude_defaults_to_opus_at_medium_effort(tmp_path):
    assert "--model opus --effort medium" in _command_line(tmp_path, {}, "claude")


def test_codex_defaults_to_sol_at_medium_effort(tmp_path):
    line = _command_line(tmp_path, {}, "codex")
    assert "-m gpt-6.1-sol" in line and 'model_reasoning_effort="medium"' in line


def _resume_line(tmp_path, agent):
    launcher, _ = init_agent._write_launcher(
        tmp_path, "m", "", [], {"XDG_RUNTIME_DIR": str(tmp_path)}, init_agent.AgentSpec(agent=agent, resume="c0ffee")
    )
    return next(line for line in launcher.read_text().splitlines() if " --name m" in line or "codex " in line)


def test_a_claude_resume_reopens_that_conversation(tmp_path):
    assert "--name m --resume c0ffee" in _resume_line(tmp_path, "claude")


def test_a_codex_resume_puts_every_flag_before_the_resume_subcommand(tmp_path):
    words = _resume_line(tmp_path, "codex").split()
    assert words[-2:] == ["resume", "c0ffee"]
    assert words.count("resume") == 1
    assert "-m" in words and "-c" in words


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
    assert init_agent.model_effort("claude", [], {}) == ("opus", "medium")
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
    assert rc == 0 and "model=fable" in out and "effort=medium" in out


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


@pytest.mark.parametrize("profile_home", [False, True])
def test_inbox_channel_registers_before_native_startup(monkeypatch, tmp_path, capsys, profile_home):
    import json

    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda requested, environ: ("claude", "requested"))
    monkeypatch.setattr(init_agent, "_launch_command", lambda *args: ("linux", ["t"]))
    environ = {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "rt")}
    config = tmp_path / ".claude.json"
    if profile_home:
        native_home = tmp_path / "master"
        native_home.mkdir()
        environ["CLAUDE_CONFIG_DIR"] = str(native_home)
        config = native_home / ".claude.json"
    config.write_text(json.dumps({"hasCompletedOnboarding": True, "mcpServers": {"existing": {"command": "existing"}}}))
    argv = ["--dir", str(tmp_path), "--agent", "claude", "--inbox-channel", "--dry-run"]
    assert init_agent.main(argv, environ) == 0
    registered = json.loads(config.read_text())
    assert registered["hasCompletedOnboarding"] is True
    assert registered["mcpServers"]["existing"] == {"command": "existing"}
    args = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("claude_args="))
    inline = shlex.split(args.removeprefix("claude_args="))[0].removeprefix("--mcp-config=")
    native = registered["mcpServers"]["inbox"]
    active = json.loads(inline)["mcpServers"]["inbox"]
    assert native["command"] == active["command"]
    assert native["args"][:-1] == active["args"][:-1]
    calls = []
    from scripts.inbox import channel

    monkeypatch.setattr(channel, "main", lambda active=True: calls.append(active))
    exec(native["args"][-1])
    exec(active["args"][-1])
    assert calls == [False, True]


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


@pytest.mark.parametrize(
    ("agent", "detected", "outcome", "named"),
    [
        ("codex", True, "named", ["agent_name=engineer-abc-0001", "session_name=engineer@abc-0001"]),
        (
            "codex",
            True,
            "unconfirmed",
            ["agent_name=engineer-abc-0001", "session_name=unset", "session_name_error=unconfirmed"],
        ),
        ("codex", False, "named", ["agent_name=unset"]),
        ("claude", True, "named", ["agent_name=engineer-abc-0001"]),
    ],
)
def test_herdr_names_the_agent_and_a_codex_session_by_its_raw_name(monkeypatch, agent, detected, outcome, named):
    calls, marked = [], []
    monkeypatch.setattr(init_agent.herdr_host, "rename_agent", lambda *args: calls.append(("agent", *args)) or detected)
    monkeypatch.setattr(init_agent.herdr_host, "wait_ready", lambda *args: calls.append(("ready", *args)) or True)
    monkeypatch.setattr(init_agent, "_codex_named", lambda environ, name, since: ("index", environ, name, since))
    monkeypatch.setattr(
        init_agent.herdr_host, "name_session", lambda *args: calls.append(("session", *args)) or outcome
    )
    monkeypatch.setattr(
        init_agent.herdr_panes, "mark", lambda pane, environ, **changes: marked.append((pane, environ, changes))
    )
    assert (
        init_agent._name_in_herdr(
            "w1:p2", init_agent.Names("engineer@abc-0001"), agent, {"K": "v"}, "", init_agent.Launch(12.5)
        )
        == named
    )
    expected = [("agent", "w1:p2", "engineer@abc-0001", {"K": "v"})]
    if agent == "codex" and detected:
        expected.append(("ready", "w1:p2", {"K": "v"}, init_agent.CODEX_READY_S))
        expected.append(
            ("session", "w1:p2", "engineer@abc-0001", {"K": "v"}, ("index", {"K": "v"}, "engineer@abc-0001", 12.5))
        )
    assert calls == expected
    session = {"named": "engineer@abc-0001", "unconfirmed": "unset:unconfirmed"}[outcome]
    assert marked == ([("w1:p2", {"K": "v"}, {"session_name": session})] if agent == "codex" and detected else [])
    assert init_agent.CODEX_READY_S == 120.0


def test_an_app_server_viewer_gets_no_typed_rename_because_its_server_names_the_thread(monkeypatch):
    monkeypatch.setattr(init_agent.herdr_host, "rename_agent", lambda *args: True)
    monkeypatch.setattr(init_agent.herdr_host, "wait_ready", lambda *args: pytest.fail("waited on a served viewer"))
    monkeypatch.setattr(init_agent.herdr_host, "name_session", lambda *args: pytest.fail("typed a rename"))
    monkeypatch.setattr(init_agent.herdr_panes, "mark", lambda *args, **changes: pytest.fail("marked a session name"))
    assert init_agent._name_in_herdr(
        "w1:p2", init_agent.Names("eng-a"), "codex", {}, "", init_agent.Launch(0.0), served=True
    ) == ["agent_name=eng-a"]


def test_a_codex_session_that_never_reads_ready_is_recorded_unnamed(monkeypatch):
    marked = []
    monkeypatch.setattr(init_agent.herdr_host, "rename_agent", lambda *args: True)
    monkeypatch.setattr(init_agent.herdr_host, "wait_ready", lambda *args: False)
    monkeypatch.setattr(init_agent.herdr_host, "name_session", lambda *args: pytest.fail("renamed while not ready"))
    monkeypatch.setattr(init_agent.herdr_panes, "mark", lambda pane, environ, **changes: marked.append(changes))
    assert init_agent._name_in_herdr("w1:p2", init_agent.Names("eng-a"), "codex", {}, "", init_agent.Launch(0.0)) == [
        "agent_name=eng-a",
        "session_name=unset",
        "session_name_error=not-ready",
    ]
    assert marked == [{"session_name": "unset:not-ready"}]


def _ready_codex(monkeypatch, calls, ready=True, busy=""):
    monkeypatch.setattr(init_agent.herdr_host, "rename_agent", lambda *args: calls.append("agent") or True)
    monkeypatch.setattr(init_agent.herdr_host, "wait_ready", lambda *args: calls.append("ready") or ready)
    monkeypatch.setattr(init_agent.herdr_host, "name_session", lambda *args: calls.append("session") or "named")
    monkeypatch.setattr(init_agent.herdr_host, "busy", lambda *args: calls.append(("busy", *args)) or busy)
    monkeypatch.setattr(init_agent, "_codex_named", lambda environ, name, since: None)
    monkeypatch.setattr(init_agent.herdr_panes, "mark", lambda pane, environ, **changes: None)


@pytest.mark.parametrize(("sent", "said"), [(True, "sent"), (False, "failed")])
def test_the_opening_prompt_follows_the_names(monkeypatch, sent, said):
    calls = []
    _ready_codex(monkeypatch, calls)
    monkeypatch.setattr(init_agent.herdr_host, "prompt", lambda *args: calls.append(("prompt", *args)) or sent)
    named = init_agent._name_in_herdr(
        "w1:p2", init_agent.Names("eng-a"), "codex", {"K": "v"}, "Start.", init_agent.Launch(0.0)
    )
    assert named == ["agent_name=eng-a", "session_name=eng-a", f"opening_prompt={said}"]
    assert calls == [
        "agent",
        "ready",
        "session",
        "ready",
        ("busy", "w1:p2", {"K": "v"}),
        ("prompt", "w1:p2", "Start.", {"K": "v"}),
    ]


@pytest.mark.parametrize(("ready", "busy", "said"), [(False, "", "held:not-ready"), (True, "typed", "held:typed")])
def test_an_opening_prompt_is_held_from_a_busy_pane(monkeypatch, ready, busy, said):
    calls = []
    _ready_codex(monkeypatch, calls, ready, busy)
    monkeypatch.setattr(init_agent.herdr_host, "prompt", lambda *args: pytest.fail("prompted a busy pane"))
    named = init_agent._name_in_herdr("w1:p2", init_agent.Names("eng-a"), "codex", {}, "Start.", init_agent.Launch(0.0))
    assert named[-1] == f"opening_prompt={said}"
    assert [call for call in calls if call[0] == "busy"] == ([("busy", "w1:p2", {})] if ready else [])


@pytest.mark.parametrize(
    ("status", "agent", "held", "names"),
    [
        ("routed", "claude", False, True),
        ("bare", "codex", False, True),
        ("direct", "codex", False, True),
        (None, "codex", True, True),
        (None, "codex", False, False),
        (None, "claude", True, False),
        ("failed", "codex", True, False),
    ],
)
def test_a_pane_is_named_once_routed_or_while_a_held_prompt_waits_on_the_route(tmp_path, status, agent, held, names):
    launcher = tmp_path / "eng.sh"
    if held:
        launcher.with_suffix(".prompt").write_text("Start.", encoding="utf-8")
    route = {"status": status} if status else {}
    assert init_agent._names_pane(route, launcher, agent) is names


def test_an_undetected_agent_still_gets_its_opening_prompt_after_the_ready_wait(monkeypatch):
    calls = []
    monkeypatch.setattr(init_agent.herdr_host, "rename_agent", lambda *args: False)
    monkeypatch.setattr(init_agent.herdr_host, "wait_ready", lambda *args: calls.append(("ready", *args)) or True)
    monkeypatch.setattr(init_agent.herdr_host, "busy", lambda *args: "")
    monkeypatch.setattr(init_agent.herdr_host, "prompt", lambda *args: calls.append(("prompt", *args)) or True)
    assert init_agent._name_in_herdr(
        "w1:p2", init_agent.Names("eng-a"), "codex", {}, "Start.", init_agent.Launch(0.0)
    ) == [
        "agent_name=unset",
        "opening_prompt=sent",
    ]
    assert calls == [
        ("ready", "w1:p2", {}, init_agent.CODEX_READY_S),
        ("ready", "w1:p2", {}, init_agent.CODEX_READY_S),
        ("prompt", "w1:p2", "Start.", {}),
    ]


def test_an_undetected_codex_agent_without_a_prompt_waits_for_nothing(monkeypatch):
    monkeypatch.setattr(init_agent.herdr_host, "rename_agent", lambda *args: False)
    monkeypatch.setattr(init_agent.herdr_host, "wait_ready", lambda *args: pytest.fail("waited"))
    assert init_agent._name_in_herdr("w1:p2", init_agent.Names("eng-a"), "codex", {}, "", init_agent.Launch(0.0)) == [
        "agent_name=unset"
    ]


@pytest.mark.parametrize(("held", "argv"), [("1", "<a b>\n"), ("", "<a b><Start the task.>\n")])
def test_the_codex_launcher_line_runs_its_command_without_the_prompt_only_when_held(tmp_path, held, argv):
    spec = init_agent.AgentSpec(agent="codex")
    launcher, _ = init_agent._write_launcher(
        tmp_path, "eng", "Start the task.", [], {"XDG_RUNTIME_DIR": str(tmp_path)}, spec
    )
    line = next(x for x in launcher.read_text().splitlines() if x.startswith("set --; [ -n"))
    command = line.split("; unset AGENTIHOOKS_PROMPT_AFTER_START; ", 1)[1].removesuffix(' "$@"')
    script = line.replace(command, "printf '<%s>' 'a b'")
    done = subprocess.run(
        ["bash", "-c", f'{script}; echo; echo "left=${{AGENTIHOOKS_PROMPT_AFTER_START:-}}"'],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], **({"AGENTIHOOKS_PROMPT_AFTER_START": held} if held else {})},
    )
    assert done.stdout == f"{argv}left=\n"


def _index(home, *rows):
    home.mkdir(parents=True, exist_ok=True)
    junk = "not json\n5\nnull\n[]\n" + json.dumps({"thread_name": "eng-a"}) + "\n"
    (home / "session_index.jsonl").write_text(junk + "".join(json.dumps(row) + "\n" for row in rows))


def test_a_codex_name_is_confirmed_from_its_session_index_after_the_launch(tmp_path):
    since = calendar.timegm((2026, 10, 12, 2, 43, 50, 0, 0, 0))
    home = tmp_path / "codex"
    environ = {"CODEX_HOME": str(home)}
    named = init_agent._codex_named(environ, "eng-a", since)
    assert named() is False
    _index(home, {"id": "a", "thread_name": "eng-a", "updated_at": "2026-10-12T02:43:49.9Z"})
    assert named() is False
    _index(home, {"id": "a", "thread_name": "eng-a", "updated_at": "2026-10-12T02:43:50"})
    assert named() is True
    _index(
        home,
        {"id": "b", "thread_name": "eng-b", "updated_at": "2026-10-12T02:43:58.92Z"},
        {"id": "c", "thread_name": "eng-a", "updated_at": "2026-10-12T02:43:50.000000001Z"},
    )
    assert named() is True


def test_a_codex_launcher_holds_its_prompt_behind_the_herdr_variable(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda requested, environ: ("codex", "requested"))
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: "/usr/bin/agentihooks")
    runtime = tmp_path / "rt"
    argv = ["--dir", str(tmp_path), "--name", "eng-a", "--prompt", "Start.", "--host", "herdr", "--dry-run"]
    assert init_agent.main(argv, {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(runtime)}) == 0
    launcher = next((runtime / "agentihooks-claude-terminal").glob("*.sh")).read_text().splitlines()
    prompt_file = next((runtime / "agentihooks-claude-terminal").glob("*.prompt"))
    line = next(x for x in launcher if x.startswith("set --; [ -n"))
    command = line.split("; unset AGENTIHOOKS_PROMPT_AFTER_START; ", 1)[1].removesuffix(' "$@"')
    assert command.startswith("/usr/bin/agentihooks codex ") and command.count("agentihooks codex") == 1
    assert line == (
        f'set --; [ -n "${{AGENTIHOOKS_PROMPT_AFTER_START:-}}" ] || set -- "$(cat {prompt_file})"; '
        f'unset AGENTIHOOKS_PROMPT_AFTER_START; {command} "$@"'
    )
    assert init_agent.PROMPT_AFTER_START == "AGENTIHOOKS_PROMPT_AFTER_START"


@pytest.mark.parametrize("prompt", ["Start.", "x" * init_agent.PROMPT_ARGUMENT_LIMIT])
def test_a_claude_launcher_separates_its_prompt_from_a_trailing_config_option(monkeypatch, tmp_path, prompt):
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda requested, environ: ("claude", "requested"))
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: "/usr/bin/agentihooks")
    monkeypatch.setattr(init_agent, "_launch_command", lambda launcher, directory, title, environ: ("linux", ["t"]))
    runtime = tmp_path / "rt"
    config = tmp_path / "mcp.json"
    argv = ["--dir", str(tmp_path), "--name", "eng-a", "--prompt", prompt, "--dry-run", "--", "--mcp-config"]
    assert init_agent.main([*argv, str(config)], {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(runtime)}) == 0
    folder = runtime / "agentihooks-claude-terminal"
    prompt_file = next(folder.glob("*.prompt"))
    launcher = next(folder.glob("*.sh")).read_text()
    written = prompt_file.read_text()
    opening = init_agent._opening_prompt(written, prompt_file)
    argument = f'"$(cat {prompt_file})"' if opening == written else shlex.quote(opening)
    assert f" --mcp-config {config} -- {argument}\nnative_status=$?\n" in launcher


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
@pytest.mark.parametrize("source,target", [("codex", "claude"), ("claude", "codex")])
def test_a_subscription_handoff_never_crosses_harnesses(monkeypatch, tmp_path, capsys, extra, source, target):
    monkeypatch.setattr(init_agent.operator_env, "fill", lambda env: None)
    assert (
        init_agent.main(
            ["--dir", str(tmp_path), "--agent", target, "--handoff", "--prompt", "saved task", "--dry-run", *extra],
            {"HOME": str(tmp_path), "AGENTIHOOKS_TARGET": source},
        )
        == 2
    )
    assert (
        f"unsupported quota transfer: {source.capitalize()} cannot transfer to a {target.capitalize()} account"
        in capsys.readouterr().err
    )


@pytest.mark.parametrize(
    "requested,source,agent",
    [("", "copilot", "claude"), ("copilot", "claude", "claude"), ("", "codex", "codex"), ("codex", "codex", "codex")],
)
def test_only_codex_sources_hand_off_to_codex(requested, source, agent):
    assert init_agent._handoff_agent(requested, {"AGENTIHOOKS_TARGET": source}, []) == agent


@pytest.mark.parametrize(
    "requested,environ,harness,agent",
    [
        ("codex", {}, "codex", "codex"),
        ("", {}, "codex", "codex"),
        ("", {}, "claude", "claude"),
        ("", {"AGENTIHOOKS_TARGET": "codex"}, "claude", "codex"),
    ],
)
def test_a_handoff_without_a_target_takes_the_live_harness(monkeypatch, requested, environ, harness, agent):
    from scripts.profiles import binding

    monkeypatch.setattr(binding, "process", lambda: (123, harness, {}, "alpha"))
    assert init_agent._handoff_agent(requested, environ, []) == agent


@pytest.mark.parametrize("error", [ValueError("no supported live harness ancestor"), OSError("process vanished")])
def test_a_handoff_outside_any_harness_comes_from_claude(monkeypatch, error):
    from scripts.profiles import binding

    def missing():
        raise error

    monkeypatch.setattr(binding, "process", missing)
    assert init_agent._live_harness() == "claude"
    assert init_agent._handoff_agent("", {}, []) == "claude"
    with pytest.raises(ValueError, match="unsupported quota transfer: Claude cannot transfer to a Codex account"):
        init_agent._handoff_agent("codex", {}, [])


def test_a_handoff_excludes_the_account_it_leaves(monkeypatch):
    from scripts.profiles import binding

    monkeypatch.setattr(binding, "process", lambda: (123, "codex", {}, "alpha"))
    assert init_agent._handoff_exclude("codex", {"AH_CX_TOKEN_beta": "b"}) == "alpha"
    assert init_agent._handoff_exclude("claude", {"AH_CC_TOKEN_beta": "b"}) == "beta"
    assert init_agent._handoff_exclude("claude", {}) == ""


@pytest.mark.parametrize("explicit", [False, True])
def test_a_codex_handoff_resumes_the_conversation_on_another_codex_account(monkeypatch, tmp_path, capsys, explicit):
    binding, profile_env = _profile(monkeypatch, tmp_path, "codex")
    original = {**profile_env, "AGENTIHOOKS_RUN_MODEL": "gpt-6.1-sol", "AGENTIHOOKS_RUN_EFFORT": "medium"}
    monkeypatch.setattr(binding, "process", lambda: (123, "codex", original, "alpha"))
    monkeypatch.setattr(init_agent, "_launch_command", lambda *args: ("linux", ["terminal"]))
    monkeypatch.setattr(init_agent.operator_env, "fill", lambda env: None)
    argv = ["--dir", str(tmp_path), "--name", "cx-handoff", *(["--agent", "codex"] if explicit else [])]
    argv += ["--prompt", "saved task", "--handoff", "--resume", "thread-1", "--dry-run"]
    environ = {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "runtime"), "AGENTIHOOKS_TARGET": "codex"}
    assert init_agent.main(argv, {**environ, "AH_CX_TOKEN_alpha": "tok", "AH_CX_TOKEN_beta": "tok", **profile_env}) == 0
    report = capsys.readouterr().out
    assert "agent=codex\n" in report and "agent_reason=handoff\n" in report
    launcher = next(line.split("=", 1)[1] for line in report.splitlines() if line.startswith("launcher="))
    command = Path(launcher).read_text()
    home = profile_env["CODEX_HOME"]
    assert (
        f"select-profile engineer --home={home} --agent codex -- --agentihooks-exclude alpha --agentihooks-report"
        in command
    )
    assert " resume thread-1 " in command
    assert "-m gpt-6.1-sol" in command


@pytest.mark.parametrize("source_flags", [False, True, "session", "single", "partial", "defaults", "override"])
def test_a_plain_codex_quota_handoff_preserves_native_choices(monkeypatch, tmp_path, capsys, source_flags):
    from scripts.profiles import binding

    home = tmp_path / "codex"
    home.mkdir()
    config = 'model = "gpt-6-astra"\nmodel_reasoning_effort = "low"\n'
    (home / "config.toml").write_text("" if source_flags == "defaults" else config)
    source = {"CODEX_HOME": str(home)}
    if source_flags is True:
        source.update(AGENTIHOOKS_RUN_MODEL="gpt-6.1-sol", AGENTIHOOKS_RUN_EFFORT="high")
    rollout = tmp_path / "rollout.jsonl"
    latest = '{"type":"turn_context","payload":{"model":"gpt-6-sol","effort":"medium"}}\n'
    transcripts = {
        "single": latest,
        "partial": '{"type":"turn_context","payload":{}}\n',
        "session": (
            '{"type":"turn_context","payload":{"model":"gpt-6-astra","effort":"low"}}\n'
            + latest
            + '{"type":"event_msg","payload":{"model":"unrelated","effort":"low"}}\n'
            + "not-json\n"
        ),
    }
    rollout.write_text(transcripts.get(source_flags, ""))
    monkeypatch.setattr(
        "hooks.targets.normalizer.codex_rollout_path",
        lambda session: str(rollout) if source_flags in transcripts and session == "thread-1" else "",
    )
    monkeypatch.setattr(binding, "process", lambda: (123, "codex", source, "alpha"))
    monkeypatch.setattr(init_agent.operator_env, "fill", lambda env: None)
    monkeypatch.setattr(init_agent, "_launch_command", lambda launcher, *args: ("linux", ["terminal", str(launcher)]))
    launched = []

    def popen(command, **kwargs):
        launcher = Path(command[-1])
        launched.append((launcher.read_text(), kwargs["env"]))
        _routed("routed", "beta")(command, **kwargs)

    monkeypatch.setattr(init_agent.subprocess, "Popen", popen)
    monkeypatch.setattr(init_agent, "_mark_handoff", lambda account, resume: ["thread-1"])
    env = {
        "HOME": str(tmp_path),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        "AGENTIHOOKS_TARGET": "codex",
        "CODEX_HOME": str(home),
        "AH_CX_TOKEN_alpha": "tok",
    }
    flags = ["--", "-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="xhigh"', "--no-alt-screen"]
    assert (
        init_agent.main(
            [
                "--dir",
                str(tmp_path),
                "--name",
                "plain-handoff",
                "--prompt",
                "saved task",
                "--handoff",
                "--resume",
                "thread-1",
                "--start-timeout",
                "0",
                "--route-timeout",
                "0",
                *(flags if source_flags == "override" else []),
            ],
            env,
        )
        == 0
    )
    launcher, successor = launched[0]
    command = next(line for line in launcher.splitlines() if " codex " in line)
    assert "--agentihooks-exclude alpha" in command
    assert "resume thread-1" in command
    assert "select-profile" not in command
    assert "AGENTIHOOKS_PROFILE_REPORT" not in successor
    assert not successor.get("AGENTIHOOKS_PROFILE")
    expected_model, expected_effort = {
        False: ("gpt-6-astra", "low"),
        True: ("gpt-6.1-sol", "high"),
        "session": ("gpt-6-sol", "medium"),
        "single": ("gpt-6-sol", "medium"),
        "partial": ("gpt-6-astra", "low"),
        "defaults": ("", ""),
        "override": ("gpt-6.1-sol", "xhigh"),
    }[source_flags]
    if expected_model:
        assert f"-m {expected_model}" in command
        assert f'model_reasoning_effort="{expected_effort}"' in command
        assert command.count("model_reasoning_effort=") == 1
    else:
        assert " -m " not in command
        assert "model_reasoning_effort=" not in command
    if source_flags == "override":
        assert "--no-alt-screen" in command
    report = capsys.readouterr().out
    assert "handoff=done" in report
    assert "account=beta" in report
    assert "profile_validation" not in report


def test_a_plain_codex_handoff_refuses_a_different_live_harness(monkeypatch, tmp_path, capsys):
    from scripts.profiles import binding

    monkeypatch.setattr(binding, "process", lambda: (123, "claude", {}, "alpha"))
    monkeypatch.setattr(init_agent.operator_env, "fill", lambda env: None)
    monkeypatch.setattr(init_agent.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not launch"))
    assert (
        init_agent.main(
            ["--dir", str(tmp_path), "--agent", "codex", "--prompt", "saved task", "--handoff"],
            {"HOME": str(tmp_path), "AGENTIHOOKS_TARGET": "codex"},
        )
        == 2
    )
    assert "unsupported quota transfer from claude to codex" in capsys.readouterr().err
