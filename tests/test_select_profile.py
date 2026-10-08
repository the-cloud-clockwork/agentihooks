import json
from unittest.mock import Mock

import pytest

from scripts import init_agent, select_profile
from scripts.profiles import binding


@pytest.fixture
def profile(monkeypatch, tmp_path):
    root = tmp_path / "engineer"
    root.mkdir()
    (root / "profile.yml").write_text("model: sonnet\neffort: medium\n")
    renderer = Mock()
    from scripts.targets._common import _install_module

    monkeypatch.setattr(_install_module(), "_resolve_profile_chain", lambda name: [(name, root)])
    monkeypatch.setattr(select_profile.profiles, "_chain", lambda name: [(name, root)])
    monkeypatch.setattr(select_profile.profiles, "render", renderer)
    monkeypatch.setattr(
        select_profile.profiles, "profile_dir", lambda name, worn=(): tmp_path / "rendered" / "+".join([name, *worn])
    )
    monkeypatch.setattr(select_profile.profiles, "channels", {"engineer": "amygdala,brain", "qa": "amygdala,brain"}.get)
    return tmp_path, renderer


def test_dry_run_parses_run_flags_and_preserves_harness_arguments(profile, capsys):
    root, renderer = profile
    assert (
        select_profile.main(["engineer", "--model", "opus", "--effort", "low", "--dry-run", "--", "-p", "reply OK"])
        == 0
    )
    assert capsys.readouterr().out == (
        "AGENTIHOOKS_PROFILE=engineer\n"
        "AGENTIHOOKS_BASE_CHANNELS=amygdala,brain\n"
        "AGENTIHOOKS_OVERLAYS=\n"
        "AGENTIHOOKS_BUNDLE_REVISION=\n"
        f"CLAUDE_CONFIG_DIR={root}/rendered/engineer/claude\n"
        "argv=agentihooks claude --model opus --effort low -p 'reply OK'\n"
    )
    renderer.assert_called_once_with("claude", "engineer", overlays=[], bundle_revision="")


def test_profile_defaults_and_native_codex_layer(profile):
    root, renderer = profile
    env, argv = select_profile.prepare("qa", "codex", "", "", ["exec", "reply OK"], {})
    assert env == {
        "AGENTIHOOKS_PROFILE": "qa",
        "AGENTIHOOKS_BASE_CHANNELS": "amygdala,brain",
        "AGENTIHOOKS_OVERLAYS": "",
        "AGENTIHOOKS_BUNDLE_REVISION": "",
        "CODEX_HOME": f"{root}/rendered/qa/codex",
    }
    assert argv == ["-m", "sonnet", "-c", 'model_reasoning_effort="medium"', "exec", "reply OK"]
    renderer.assert_called_once_with("codex", "qa", overlays=(), bundle_revision="")


@pytest.mark.parametrize("agent,effort,mapped", [("claude", "minimal", "low"), ("codex", "max", "xhigh")])
def test_effort_mapping(profile, capsys, agent, effort, mapped):
    _, argv = select_profile.prepare("engineer", agent, "m", effort, [], {})
    assert init_agent.model_flags(agent, "m", mapped) == argv[-4:]
    assert capsys.readouterr().err == f"agentihooks select-profile: {agent} effort {effort} maps to {mapped}\n"


def test_copilot_refused_before_render(profile, capsys):
    assert select_profile.main(["qa", "--agent", "copilot"]) == 2
    assert capsys.readouterr().err == "agentihooks select-profile: copilot per-run profiles are not supported\n"
    profile[1].assert_not_called()


@pytest.mark.parametrize("state", ["pending", "validated", "failed"])
def test_a_failed_selection_fails_only_a_pending_launch_report(profile, monkeypatch, state):
    root, _ = profile
    report = root / "report.json"
    requested = {"profile": "qa", "harness": "copilot", "state": state}
    report.write_text(json.dumps(requested))
    monkeypatch.setenv(binding.REPORT, str(report))
    assert select_profile.main(["qa", "--agent", "copilot"]) == 2
    if state == "pending":
        requested.update(state="failed", reason="profile selection failed: copilot per-run profiles are not supported")
    assert json.loads(report.read_text()) == requested


def test_a_failed_selection_without_a_launch_report_writes_nothing(profile, monkeypatch):
    root, _ = profile
    monkeypatch.delenv(binding.REPORT, raising=False)
    before = sorted(root.rglob("*"))
    assert select_profile.main(["qa", "--agent", "copilot"]) == 2
    assert sorted(root.rglob("*")) == before


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
    assert result == expected
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
    assert result == ["-m", "env-model", "-c", 'model_reasoning_effort="minimal"']


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
        capsys.readouterr().out == "AGENTIHOOKS_PROFILE=qa\nAGENTIHOOKS_BASE_CHANNELS=amygdala,brain\n"
        "AGENTIHOOKS_OVERLAYS=\n"
        "AGENTIHOOKS_BUNDLE_REVISION=\n"
        f"CODEX_HOME={profile[0]}/rendered/qa/codex\n"
        "argv=agentihooks codex -m sonnet -c 'model_reasoning_effort=\"medium\"' exec OK\n"
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


@pytest.mark.parametrize("args", [["-c"], ["-c", "continue prompt"]])
def test_claude_continue_is_forwarded(profile, args):
    _, result = select_profile.prepare("engineer", "claude", "", "", args, {})
    assert result == ["--model", "sonnet", "--effort", "medium", *args]


@pytest.mark.parametrize("flag", ['-cmodel_reasoning_effort="low"', '--config=model_reasoning_effort="low"'])
def test_attached_codex_effort_wins_over_manifest(profile, flag):
    _, result = select_profile.prepare("qa", "codex", "", "", [flag, "exec", "OK"], {})
    assert result == ["-m", "sonnet", "-c", 'model_reasoning_effort="low"', "exec", "OK"]


def test_attached_codex_model_cannot_override_selector(profile):
    _, result = select_profile.prepare("qa", "codex", "chosen", "", ["-mnative", "exec", "OK"], {})
    assert result == ["-m", "chosen", "-c", 'model_reasoning_effort="medium"', "exec", "OK"]


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_profile_command_keeps_executable_and_has_no_before_code(monkeypatch, tmp_path, agent):
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: "/bin/agentihooks")
    command, before = init_agent._agent_command(
        init_agent.AgentSpec(agent=agent, profile="engineer"), tmp_path / "route", "run", [], {}, tmp_path
    )
    assert command[:6] == ["/bin/agentihooks", "select-profile", "engineer", "--agent", agent, "--"]
    assert before == ""


def test_terminal_profile_usage(capsys):
    assert init_agent._parser().parse_args([]).profile == ""
    with pytest.raises(SystemExit) as exc:
        init_agent.main(["--help"])
    assert exc.value.code == 0
    assert "--profile PROFILE Role profile for this run" in " ".join(capsys.readouterr().out.split())


@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high", "xhigh", "max"])
def test_cli_accepts_all_efforts_for_explicit_claude(profile, capsys, effort):
    assert select_profile.main(["engineer", "--agent", "claude", "--effort", effort, "--dry-run"]) == 0
    assert "--effort " + ("low" if effort == "minimal" else effort) in capsys.readouterr().out


def test_invalid_agent_is_a_usage_error(profile, capsys):
    with pytest.raises(SystemExit) as exc:
        select_profile.main(["engineer", "--agent", "invalid"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize("args", [["-m"], ["--model"]])
def test_missing_native_model_is_usage_error(profile, capsys, args):
    with pytest.raises(SystemExit) as exc:
        select_profile.main(["engineer", "--", *args])
    assert exc.value.code == 2
    assert "expected one argument" in capsys.readouterr().err


@pytest.mark.parametrize("args", [["-c"], ["--config"]])
def test_missing_codex_config_is_reported(profile, capsys, args):
    assert select_profile.main(["qa", "--agent", "codex", "--", *args]) == 2
    assert "requires a value" in capsys.readouterr().err


def test_defaults_resolve_the_requested_profile(profile, monkeypatch):
    root, _ = profile
    chain = Mock(return_value=[("engineer", root / "engineer")])
    monkeypatch.setattr(select_profile.profiles, "_chain", chain)
    select_profile.prepare("engineer", "claude", "", "", [], {})
    chain.assert_called_once_with("engineer")


def test_terminal_profile_prepares_model_and_environment_once(profile, monkeypatch, tmp_path):
    prepare = Mock(
        return_value=(
            {"AGENTIHOOKS_PROFILE": "qa"},
            ["-m", "selected", "-c", 'model_reasoning_effort="low"'],
        )
    )
    launch = Mock(return_value=(tmp_path / "launcher", None))
    monkeypatch.setattr(select_profile, "prepare", prepare)
    monkeypatch.setattr(init_agent, "_write_launcher", launch)
    monkeypatch.setattr(init_agent, "_launch_command", lambda *args: ("linux", ["terminal"]))
    env = {"HOME": str(tmp_path)}
    assert init_agent.main(["--profile", "qa", "--agent", "codex", "--dir", str(tmp_path), "--dry-run"], env) == 0
    chosen = {**env, "AGENTIHOOKS_BUNDLE_REVISION": "", "AGENTIHOOKS_PROFILE": "qa"}
    prepare.assert_called_once_with("qa", "codex", "", "", [], chosen, [])
    assert launch.call_args.args[3] == ["-m", "selected", "-c", 'model_reasoning_effort="low"']
    assert launch.call_args.args[4] == chosen


def test_installer_help_has_exact_selector_entry(monkeypatch, capsys):
    from scripts import install

    monkeypatch.setattr(install.sys, "argv", ["agentihooks", "--help"])
    with pytest.raises(SystemExit):
        install.main()
    assert "select-profile Select a profile, model and effort for one routed run" in " ".join(
        capsys.readouterr().out.split()
    )


def test_selector_usage_names_command_exactly(capsys):
    with pytest.raises(SystemExit):
        select_profile.main(["--help"])
    assert capsys.readouterr().out.startswith("usage: agentihooks select-profile ")


@pytest.mark.parametrize("args", [["--help"], ["-h"], ["--mode", "unrelated"], ["--eff", "unrelated"]])
def test_native_help_and_abbreviations_are_forwarded(profile, args):
    _, result = select_profile.prepare("engineer", "claude", "", "", args, {})
    assert result == ["--model", "sonnet", "--effort", "medium", *args]


@pytest.mark.parametrize("flag", ["-cother=true", "--config=other=true"])
def test_other_attached_codex_config_preserves_order(profile, flag):
    _, result = select_profile.prepare("qa", "codex", "", "", ["exec", flag, "OK"], {})
    assert result == ["-m", "sonnet", "-c", 'model_reasoning_effort="medium"', "exec", flag, "OK"]


def test_profile_render_dispatch_forwards_arguments_and_exit(monkeypatch):
    main = Mock(return_value=7)
    monkeypatch.setattr(select_profile.profiles, "main", main)
    assert select_profile.dispatch(["profile", "render", "engineer", "--target", "claude"]) == 7
    main.assert_called_once_with(["render", "engineer", "--target", "claude"])


@pytest.mark.parametrize("resume", [False, True])
def test_init_agent_refuses_claude_only_profile_before_render_or_launch(profile, monkeypatch, tmp_path, capsys, resume):
    root, renderer = profile
    settings = root / "engineer" / ".claude" / "settings.overrides.json"
    settings.parent.mkdir()
    settings.write_text('{"enabledPlugins": {"frontend-design@claude-plugins-official": true}}')
    launcher = Mock()
    monkeypatch.setattr(init_agent, "_write_launcher", launcher)
    flags = ["--resume", "saved-session"] if resume else []
    result = init_agent.main(
        ["--profile", "frontend", "--agent", "codex", "--dir", str(tmp_path), *flags],
        {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path)},
    )
    assert result == 2
    assert capsys.readouterr().err == (
        "agentihooks init-agent: profile frontend does not support codex; supported harness: claude\n"
    )
    renderer.assert_not_called()
    launcher.assert_not_called()


@pytest.mark.parametrize("agent,enabled", [("claude", True), ("codex", False)])
def test_init_agent_allows_supported_profile_harness_pair(profile, tmp_path, capsys, agent, enabled):
    root, renderer = profile
    settings = root / "engineer" / ".claude" / "settings.overrides.json"
    settings.parent.mkdir()
    settings.write_text('{"enabledPlugins": {"frontend-design@claude-plugins-official": ' + str(enabled).lower() + "}}")
    assert (
        init_agent.main(
            ["--profile", "engineer", "--agent", agent, "--dir", str(tmp_path), "--host", "herdr", "--dry-run"],
            {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path)},
        )
        == 0
    )
    renderer.assert_called_once_with(agent, "engineer", overlays=[], bundle_revision="")
    output = capsys.readouterr()
    assert output.err == ""
    assert f"agent={agent}\n" in output.out
    assert "profile=engineer\n" in output.out
    assert "status=dry-run\n" in output.out


def test_a_dependency_restart_relaunches_through_the_original_profile(profile, monkeypatch, tmp_path, capsys):
    from hooks.lifecycle import refresh

    original = refresh.Original("sid-1", "eng-a", str(tmp_path), "", 4242, "engineer", "opus", "high")
    _, launch = refresh.restart_commands(original)
    monkeypatch.setattr(init_agent, "_launch_command", lambda *args: ("linux", ["terminal"]))
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: "/bin/agentihooks" if name == "agentihooks" else None)
    assert init_agent.main(["--dry-run", *launch[2:]], {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path)}) == 0
    text = next((tmp_path / "agentihooks-claude-terminal").glob("*.sh")).read_text()
    assert "select-profile engineer --agent claude --" in text
    assert "--model opus --effort high" in text
    assert "--resume sid-1" in text
    assert "profile=engineer" in capsys.readouterr().out


def test_dry_run_wears_each_overlay_in_its_own_home(profile, capsys):
    root, renderer = profile
    assert select_profile.main(["engineer", "--overlay", "tuner", "--overlay=trader", "--dry-run"]) == 0
    assert capsys.readouterr().out == (
        "AGENTIHOOKS_PROFILE=engineer\n"
        "AGENTIHOOKS_BASE_CHANNELS=amygdala,brain\n"
        "AGENTIHOOKS_OVERLAYS=tuner,trader\n"
        "AGENTIHOOKS_BUNDLE_REVISION=\n"
        f"CLAUDE_CONFIG_DIR={root}/rendered/engineer+tuner+trader/claude\n"
        "argv=agentihooks claude --model sonnet --effort medium\n"
    )
    renderer.assert_called_once_with("claude", "engineer", overlays=["tuner", "trader"], bundle_revision="")


def _dry_launch(tmp_path, argv, environ=None):
    return init_agent.main(
        [*argv, "--agent", "claude", "--dir", str(tmp_path), "--dry-run"],
        {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path), **(environ or {})},
    )


def test_init_agent_passes_each_overlay_to_the_selector(profile, monkeypatch, tmp_path, capsys):
    _, renderer = profile
    monkeypatch.setattr(init_agent, "_launch_command", lambda *args: ("linux", ["terminal"]))
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: "/bin/agentihooks" if name == "agentihooks" else None)
    assert _dry_launch(tmp_path, ["--profile", "engineer", "--overlay", "tuner", "--overlay", "trader"]) == 0
    text = next((tmp_path / "agentihooks-claude-terminal").glob("*.sh")).read_text()
    assert "select-profile engineer --overlay=tuner --overlay=trader --agent claude -- " in text
    assert "export AGENTIHOOKS_OVERLAYS=tuner,trader\n" in text
    assert "overlays=tuner,trader\n" in capsys.readouterr().out
    renderer.assert_called_once_with("claude", "engineer", overlays=["tuner", "trader"], bundle_revision="")


def test_a_continued_session_wears_the_overlays_of_its_environment(profile, monkeypatch, tmp_path, capsys):
    _, renderer = profile
    monkeypatch.setattr(init_agent, "_launch_command", lambda *args: ("linux", ["terminal"]))
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: "/bin/agentihooks" if name == "agentihooks" else None)
    environ = {"AGENTIHOOKS_PROFILE": "engineer", "AGENTIHOOKS_OVERLAYS": "tuner,trader"}
    assert _dry_launch(tmp_path, ["--resume", "conversation"], environ) == 0
    renderer.assert_called_once_with("claude", "engineer", overlays=["tuner", "trader"], bundle_revision="")
    assert "overlays=tuner,trader\n" in capsys.readouterr().out


def test_a_launch_without_overlays_clears_overlays_the_caller_wears(profile, monkeypatch, tmp_path):
    monkeypatch.setattr(init_agent, "_launch_command", lambda *args: ("linux", ["terminal"]))
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: "/bin/agentihooks" if name == "agentihooks" else None)
    assert _dry_launch(tmp_path, ["--profile", "engineer"], {"AGENTIHOOKS_OVERLAYS": "tuner"}) == 0
    text = next((tmp_path / "agentihooks-claude-terminal").glob("*.sh")).read_text()
    assert "export AGENTIHOOKS_OVERLAYS=''\n" in text
    assert "export AGENTIHOOKS_OVERLAYS=tuner" not in text


def test_an_inherited_profile_without_overlays_exports_an_empty_list(monkeypatch, tmp_path):
    monkeypatch.setattr(init_agent, "_launch_command", lambda *args: ("linux", ["terminal"]))
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: "/bin/agentihooks" if name == "agentihooks" else None)
    assert _dry_launch(tmp_path, [], {"AGENTIHOOKS_PROFILE": "engineer"}) == 0
    text = next((tmp_path / "agentihooks-claude-terminal").glob("*.sh")).read_text()
    assert "export AGENTIHOOKS_PROFILE=engineer\nexport AGENTIHOOKS_OVERLAYS=''\n" in text


@pytest.mark.parametrize(
    "run,expected",
    [
        (lambda: init_agent._parser().parse_args(["--help"]), "Overlay the profile wears; repeat for up to three"),
        (lambda: select_profile.main(["engineer", "--help"]), "Wear this overlay; repeat for up to three"),
    ],
)
def test_overlay_help_reads_as_written(run, expected, capsys):
    with pytest.raises(SystemExit):
        run()
    assert f" {expected} " in f" {' '.join(capsys.readouterr().out.split())} "


def test_a_launch_without_a_profile_exports_no_overlays(monkeypatch, tmp_path):
    monkeypatch.setattr(init_agent, "_launch_command", lambda *args: ("linux", ["terminal"]))
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: "/bin/agentihooks" if name == "agentihooks" else None)
    assert _dry_launch(tmp_path, [], {"AGENTIHOOKS_OVERLAYS": "tuner"}) == 0
    text = next((tmp_path / "agentihooks-claude-terminal").glob("*.sh")).read_text()
    assert "AGENTIHOOKS_OVERLAYS" not in text


def test_a_continued_session_without_overlays_wears_none(profile, monkeypatch, tmp_path):
    _, renderer = profile
    monkeypatch.setattr(init_agent, "_launch_command", lambda *args: ("linux", ["terminal"]))
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: "/bin/agentihooks" if name == "agentihooks" else None)
    assert _dry_launch(tmp_path, ["--resume", "conversation"], {"AGENTIHOOKS_PROFILE": "engineer"}) == 0
    renderer.assert_called_once_with("claude", "engineer", overlays=[], bundle_revision="")


def test_selector_pins_the_render_to_the_recorded_bundle_commit(profile, capsys):
    _, renderer = profile
    argv = ["engineer", "--overlay", "tuner", "--bundle-revision", "abc123", "--dry-run", "--", "-p", "OK"]
    assert select_profile.main(argv) == 0
    renderer.assert_called_once_with("claude", "engineer", overlays=["tuner"], bundle_revision="abc123")


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_profile_command_passes_the_recorded_bundle_commit_to_the_selector(monkeypatch, tmp_path, agent):
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: "/bin/agentihooks")
    spec = init_agent.AgentSpec(agent=agent, profile="engineer", overlays=("tuner",), bundle_revision="abc123")
    command, _ = init_agent._agent_command(spec, tmp_path / "route", "run", [], {}, tmp_path)
    assert command[:7] == [
        "/bin/agentihooks",
        "select-profile",
        "engineer",
        "--overlay=tuner",
        "--bundle-revision=abc123",
        "--agent",
        agent,
    ]


def test_init_agent_pins_the_render_to_the_recorded_bundle_commit(profile, monkeypatch, tmp_path):
    _, renderer = profile
    monkeypatch.setattr(init_agent, "_launch_command", lambda *args: ("linux", ["terminal"]))
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: "/bin/agentihooks" if name == "agentihooks" else None)
    argv = ["--profile", "engineer", "--overlay", "tuner", "--bundle-revision", "abc123", "--agent", "claude"]
    argv += ["--dir", str(tmp_path), "--dry-run"]
    assert init_agent.main(argv, {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path)}) == 0
    renderer.assert_called_once_with("claude", "engineer", overlays=["tuner"], bundle_revision="abc123")
    launcher = next((tmp_path / "agentihooks-claude-terminal").glob("*.sh"))
    assert "select-profile engineer --overlay=tuner --bundle-revision=abc123 --agent claude --" in launcher.read_text()


def test_a_continued_session_pins_the_bundle_commit_of_its_environment(profile, monkeypatch, tmp_path):
    _, renderer = profile
    monkeypatch.setattr(init_agent, "_launch_command", lambda *args: ("linux", ["terminal"]))
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: "/bin/agentihooks" if name == "agentihooks" else None)
    environ = {"AGENTIHOOKS_PROFILE": "engineer", "AGENTIHOOKS_OVERLAYS": "tuner"}
    environ["AGENTIHOOKS_BUNDLE_REVISION"] = "abc123"
    assert _dry_launch(tmp_path, ["--resume", "conversation"], environ) == 0
    renderer.assert_called_once_with("claude", "engineer", overlays=["tuner"], bundle_revision="abc123")
    launcher = next((tmp_path / "agentihooks-claude-terminal").glob("*.sh"))
    assert "select-profile engineer --overlay=tuner --bundle-revision=abc123 --agent claude --" in launcher.read_text()


def test_a_fresh_launch_drops_the_bundle_commit_its_caller_carries(profile, monkeypatch, tmp_path):
    _, renderer = profile
    monkeypatch.setattr(init_agent, "_launch_command", lambda *args: ("linux", ["terminal"]))
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: "/bin/agentihooks" if name == "agentihooks" else None)
    assert _dry_launch(tmp_path, ["--profile", "engineer"], {"AGENTIHOOKS_BUNDLE_REVISION": "abc123"}) == 0
    renderer.assert_called_once_with("claude", "engineer", overlays=[], bundle_revision="")


def test_the_selector_ignores_a_bundle_commit_its_caller_carries(profile, monkeypatch, capsys):
    _, renderer = profile
    monkeypatch.setenv("AGENTIHOOKS_BUNDLE_REVISION", "abc123")
    assert select_profile.main(["engineer", "--dry-run"]) == 0
    renderer.assert_called_once_with("claude", "engineer", overlays=[], bundle_revision="")
    assert "AGENTIHOOKS_BUNDLE_REVISION=\n" in capsys.readouterr().out


def test_both_launch_commands_document_the_bundle_commit_flag(capsys):
    with pytest.raises(SystemExit):
        init_agent.main(["--help"])
    shown = " ".join(capsys.readouterr().out.split())
    assert "--bundle-revision BUNDLE_REVISION Render the profile only from this bundle commit" in shown
    with pytest.raises(SystemExit):
        select_profile.main(["--help"])
    shown = " ".join(capsys.readouterr().out.split())
    assert "--bundle-revision BUNDLE_REVISION Render only from this bundle commit" in shown
