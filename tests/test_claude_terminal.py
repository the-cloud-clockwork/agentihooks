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
    assert "/usr/bin/agentihooks claude --agentihooks-fallback-bare --name quota-test" in launcher_text
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
