from unittest.mock import Mock

import pytest

from scripts import init_agent, select_profile


@pytest.fixture
def profile(monkeypatch, tmp_path):
    root = tmp_path / "engineer"
    root.mkdir()
    (root / "profile.yml").write_text("model: sonnet\neffort: medium\n")
    renderer = Mock()
    monkeypatch.setattr(select_profile.profiles, "_chain", lambda name: [(name, root)])
    monkeypatch.setattr(select_profile.profiles, "render", renderer)
    monkeypatch.setattr(select_profile.profiles, "rendered_root", lambda: tmp_path / "rendered")
    return tmp_path, renderer


def test_dry_run_parses_run_flags_and_preserves_harness_arguments(profile, capsys):
    root, renderer = profile
    assert (
        select_profile.main(["engineer", "--model", "opus", "--effort", "low", "--dry-run", "--", "-p", "reply OK"])
        == 0
    )
    assert capsys.readouterr().out == (
        "AGENTIHOOKS_PROFILE=engineer\n"
        f"CLAUDE_CONFIG_DIR={root}/rendered/engineer/claude\n"
        "argv=agentihooks claude --model opus --effort low -p 'reply OK'\n"
    )
    renderer.assert_called_once_with("claude", "engineer")


def test_profile_defaults_and_native_codex_layer(profile):
    env, argv = select_profile.prepare("qa", "codex", "", "", ["exec", "reply OK"], {})
    assert env == {"AGENTIHOOKS_PROFILE": "qa"}
    assert argv == ["-p", "qa", "-m", "sonnet", "-c", 'model_reasoning_effort="medium"', "exec", "reply OK"]


@pytest.mark.parametrize("agent,effort,mapped", [("claude", "minimal", "low"), ("codex", "max", "xhigh")])
def test_effort_mapping(profile, capsys, agent, effort, mapped):
    _, argv = select_profile.prepare("engineer", agent, "m", effort, [], {})
    assert init_agent.model_flags(agent, "m", mapped) == argv[-4:]
    assert capsys.readouterr().err == f"agentihooks select-profile: {agent} effort {effort} maps to {mapped}\n"


def test_copilot_refused_before_render(profile, capsys):
    assert select_profile.main(["qa", "--agent", "copilot"]) == 2
    assert capsys.readouterr().err == "agentihooks select-profile: copilot per-run profiles are not supported\n"
    profile[1].assert_not_called()


def test_routed_launch_preserves_environment_and_exit_code(profile, monkeypatch):
    run = Mock(return_value=Mock(returncode=17))
    monkeypatch.setattr(select_profile.subprocess, "run", run)
    monkeypatch.setenv("AH_CC_TOKEN_TEST", "test-only-token")
    assert select_profile.main(["engineer", "--", "-p", "OK"]) == 17
    assert run.call_args.args[0][:2] == ["agentihooks", "claude"]
    assert run.call_args.kwargs["env"]["AH_CC_TOKEN_TEST"] == "test-only-token"
    assert run.call_args.kwargs["env"]["AGENTIHOOKS_PROFILE"] == "engineer"


def test_init_agent_profile_uses_selector_and_keeps_route_report(profile, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(init_agent, "_launch_command", lambda *args: ("linux", ["terminal"]))
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: "/bin/agentihooks" if name == "agentihooks" else None)
    assert (
        init_agent.main(
            [
                "--profile",
                "engineer",
                "--agent",
                "claude",
                "--dir",
                str(tmp_path),
                "--dry-run",
                "--",
                "--model",
                "opus",
            ],
            {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path)},
        )
        == 0
    )
    launcher = next((tmp_path / "agentihooks-claude-terminal").glob("*.sh"))
    text = launcher.read_text()
    assert "select-profile engineer --agent claude -- --agentihooks-fallback-bare --agentihooks-report" in text
    assert "--model opus --effort medium" in text
    assert "profile=engineer" in capsys.readouterr().out


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_native_flags_override_manifest_and_other_config_is_kept(profile, agent):
    args = (
        ["--model", "native", "--effort", "high"]
        if agent == "claude"
        else ["-m", "native", "-c", 'model_reasoning_effort="high"']
    )
    args += ["-c", "other=true", "--verbose"]
    env, result = select_profile.prepare("engineer", agent, "", "", args, {})
    expected = init_agent.model_flags(agent, "native", "high") + ["-c", "other=true", "--verbose"]
    assert result == (["-p", "engineer"] if agent == "codex" else []) + expected
    assert env["AGENTIHOOKS_PROFILE"] == "engineer"


def test_selector_flags_override_native_flags(profile):
    _, result = select_profile.prepare(
        "engineer", "claude", "chosen", "low", ["--model=old", "--effort=high", "-p", "OK"], {}
    )
    assert result == ["--model", "chosen", "--effort", "low", "-p", "OK"]


def test_absent_manifest_uses_launch_defaults(profile):
    root, _ = profile
    (root / "engineer" / "profile.yml").unlink()
    _, result = select_profile.prepare("engineer", "claude", "", "", [], {})
    assert result == ["--model", "opus", "--effort", "high"]


def test_chain_defaults_use_last_profile_and_ignore_empty_manifest(profile, monkeypatch):
    root, _ = profile
    parent = root / "parent"
    parent.mkdir()
    (parent / "profile.yml").write_text("model: inherited\neffort: xhigh\n")
    (root / "engineer" / "profile.yml").write_text("")
    monkeypatch.setattr(select_profile.profiles, "_chain", lambda name: [("parent", parent), (name, root / "engineer")])
    assert select_profile._defaults("engineer") == {"model": "inherited", "effort": "xhigh"}
    (root / "engineer" / "profile.yml").write_text("effort: low\n")
    assert select_profile._defaults("engineer") == {"model": "inherited", "effort": "low"}


def test_launch_environment_defaults_apply_without_manifest_values(profile):
    root, _ = profile
    (root / "engineer" / "profile.yml").write_text("")
    _, result = select_profile.prepare(
        "engineer", "codex", "", "", [], {"AGENTIHOOKS_CODEX_MODEL": "env-model", "AGENTIHOOKS_CODEX_EFFORT": "minimal"}
    )
    assert result == ["-p", "engineer", "-m", "env-model", "-c", 'model_reasoning_effort="minimal"']


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_claude_effort_passes_through(effort, capsys):
    assert select_profile._effort("claude", effort) == effort
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("args", [[], ["--help"], ["engineer", "--effort", "invalid"]])
def test_usage(args, capsys):
    with pytest.raises(SystemExit) as exc:
        select_profile.main(args)
    assert exc.value.code == (0 if args == ["--help"] else 2)
    assert "agentihooks select-profile" in "".join(capsys.readouterr())


def test_unknown_profile_reports_error(profile, monkeypatch, capsys):
    monkeypatch.setattr(select_profile.profiles, "_chain", Mock(side_effect=ValueError("Profile 'missing' not found")))
    assert select_profile.main(["missing"]) == 2
    assert capsys.readouterr().err == "agentihooks select-profile: Profile 'missing' not found\n"


def test_render_failure_reports_error(profile, capsys):
    profile[1].side_effect = OSError("cannot render")
    assert select_profile.main(["engineer"]) == 2
    assert capsys.readouterr().err == "agentihooks select-profile: cannot render\n"


def test_codex_dry_run_from_sys_argv_has_only_run_environment(profile, monkeypatch, capsys):
    monkeypatch.setattr(
        select_profile.sys, "argv", ["agentihooks", "qa", "--agent", "codex", "--dry-run", "--", "exec", "OK"]
    )
    assert select_profile.main() == 0
    assert (
        capsys.readouterr().out
        == "AGENTIHOOKS_PROFILE=qa\nargv=agentihooks codex -p qa -m sonnet -c 'model_reasoning_effort=\"medium\"' exec OK\n"
    )


def test_installer_dispatches_selector(profile, monkeypatch):
    from scripts import install

    main = Mock(return_value=9)
    monkeypatch.setattr(select_profile, "main", main)
    monkeypatch.setattr(install.sys, "argv", ["agentihooks", "select-profile", "engineer", "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        install.main()
    assert exc.value.code == 9
    main.assert_called_once_with(["engineer", "--dry-run"])


def test_installer_help_lists_selector(monkeypatch, capsys):
    from scripts import install

    monkeypatch.setattr(install.sys, "argv", ["agentihooks", "--help"])
    with pytest.raises(SystemExit) as exc:
        install.main()
    assert exc.value.code == 0
    assert "select-profile" in capsys.readouterr().out


def test_init_codex_profile_resume_preserves_route_and_trust(profile, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(init_agent, "_launch_command", lambda *args: ("linux", ["terminal"]))
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: "/bin/agentihooks" if name == "agentihooks" else None)
    assert (
        init_agent.main(
            ["--profile", "qa", "--agent", "codex", "--resume", "conversation", "--dir", str(tmp_path), "--dry-run"],
            {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path)},
        )
        == 0
    )
    launcher = next((tmp_path / "agentihooks-claude-terminal").glob("*.sh"))
    text = launcher.read_text()
    assert "select-profile qa --agent codex -- --agentihooks-report" in text
    assert "resume conversation" in text
    assert "model_reasoning_effort=" in text
    assert "trust_level" in text
    assert "profile=qa" in capsys.readouterr().out
