from pathlib import Path

import pytest

from scripts import herdr_host, init_agent


@pytest.fixture(autouse=True)
def _no_herdr(monkeypatch):
    monkeypatch.setattr(herdr_host, "binary", lambda: None)


def test_dry_run_preserves_claude_flags_and_keeps_prompt_out_of_launcher(monkeypatch, tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    runtime = tmp_path / "runtime"
    prompt = "apostrophe ' quote \" semicolon ; and $(command)"

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
        {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(runtime), "SHELL": "/bin/bash"},
    )

    assert rc == 0
    launcher = next((runtime / "agentihooks-claude-terminal").glob("*.sh"))
    launcher_text = launcher.read_text()
    route_report = launcher.with_suffix(".route")
    assert (
        f"/usr/bin/agentihooks claude --agentihooks-fallback-bare --agentihooks-report {route_report} --name quota-test"
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
            "0.5",
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
    assert not list((tmp_path / "runtime" / "agentihooks-claude-terminal").glob("*.started"))


def test_terminal_that_never_starts_the_launcher_fails_and_discards_it(monkeypatch, tmp_path, capsys):
    rc = _launch(monkeypatch, tmp_path, lambda command, **kwargs: None)

    assert rc == 2
    assert "did not start the launcher within 0.5s" in capsys.readouterr().err
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
    monkeypatch.setattr(init_agent.subprocess, "Popen", popen)
    env = {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "runtime"), "AH_CC_TOKEN_alpha": "tok"}
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
    assert "claude --agentihooks-exclude alpha --agentihooks-report" in text
    assert "--agentihooks-fallback-bare" not in text


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
    assert "handoff failed" in captured.err


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
    assert text.index("AGENTIHOOKS_AGENT_NAME") < text.index("codex -m ")


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
        "export OTEL_LANGFUSE_ENABLED=1\n",
    ):
        assert line in text
    assert text.index("OTEL_LANGFUSE_ENABLED") < text.index(" claude ")


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
