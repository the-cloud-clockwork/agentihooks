from pathlib import Path

from scripts import claude_terminal


def test_dry_run_preserves_claude_flags_and_keeps_prompt_out_of_launcher(monkeypatch, tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    runtime = tmp_path / "runtime"
    prompt = "apostrophe ' quote \" semicolon ; and $(command)"

    monkeypatch.setattr(
        claude_terminal.shutil, "which", lambda name: "/usr/bin/agentihooks" if name == "agentihooks" else None
    )
    monkeypatch.setattr(
        claude_terminal,
        "_launch_command",
        lambda launcher, directory, title, environ: ("linux", ["/usr/bin/terminal", str(launcher)]),
    )

    rc = claude_terminal.main(
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
    monkeypatch.setattr(claude_terminal.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        claude_terminal.shutil,
        "which",
        lambda name: {"wt.exe": "/mnt/c/wt.exe", "wsl.exe": "/mnt/c/WINDOWS/system32/wsl.exe"}.get(name),
    )

    host, command = claude_terminal._launch_command(
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
        "-lc",
        str(tmp_path / "launch.sh"),
    ]
    assert not any(argument.startswith("/mnt/") for argument in command[1:])


def _launch(monkeypatch, tmp_path, popen):
    monkeypatch.setattr(claude_terminal.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        claude_terminal,
        "_launch_command",
        lambda launcher, directory, title, environ: ("linux", ["/usr/bin/terminal", str(launcher)]),
    )
    monkeypatch.setattr(claude_terminal.subprocess, "Popen", popen)
    return claude_terminal.main(
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
    monkeypatch.setattr(claude_terminal.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(claude_terminal.shutil, "which", lambda name: "/usr/bin/osascript")

    host, command = claude_terminal._launch_command(tmp_path / "launch.sh", tmp_path, "session", {})

    assert host == "macos"
    assert command[:2] == ["/usr/bin/osascript", "-e"]
    assert "Terminal" in command[2]


def test_native_linux_uses_first_supported_terminal(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_terminal.platform, "system", lambda: "Linux")
    monkeypatch.setattr(claude_terminal, "_is_wsl", lambda environ: False)
    monkeypatch.setattr(
        claude_terminal.shutil,
        "which",
        lambda name: "/usr/bin/gnome-terminal" if name == "gnome-terminal" else None,
    )

    host, command = claude_terminal._launch_command(
        tmp_path / "launch.sh",
        tmp_path,
        "session",
        {"DISPLAY": ":0"},
    )

    assert host == "linux"
    assert command == ["/usr/bin/gnome-terminal", "--title", "session", "--", str(tmp_path / "launch.sh")]


def test_headless_linux_fails_clearly(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(claude_terminal.platform, "system", lambda: "Linux")
    monkeypatch.setattr(claude_terminal, "_is_wsl", lambda environ: False)

    rc = claude_terminal.main(
        ["--dir", str(tmp_path), "--dry-run"],
        {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "runtime")},
    )

    assert rc == 2
    assert "no DISPLAY or WAYLAND_DISPLAY" in capsys.readouterr().err


def test_packaged_skill_exists_and_routes_through_agentihooks():
    skill = Path(__file__).parents[1] / "profiles" / "package" / "skills" / "run-claude-terminal" / "SKILL.md"

    assert skill.is_file()
    text = skill.read_text()
    assert "name: run-claude-terminal" in text
    assert "agentihooks claude-terminal" in text


def _handoff(monkeypatch, tmp_path, popen, extra=(), env_extra=None):
    monkeypatch.setattr(
        claude_terminal.shutil, "which", lambda name: "/usr/bin/agentihooks" if name == "agentihooks" else None
    )
    monkeypatch.setattr(
        claude_terminal,
        "_launch_command",
        lambda launcher, directory, title, environ: ("linux", ["/usr/bin/terminal", str(launcher)]),
    )
    monkeypatch.setattr(claude_terminal.subprocess, "Popen", popen)
    env = {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "runtime"), "AH_CC_TOKEN_alpha": "tok"}
    env.update(env_extra or {})
    return claude_terminal.main(
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
    rc = claude_terminal.main(["--dir", str(tmp_path), "--handoff", "--dry-run"], {"HOME": str(tmp_path)})

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
