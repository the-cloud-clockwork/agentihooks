import json
import sys
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ([], ["claude", "codex"]),
        (["--target", "copilot"], ["copilot"]),
        (["--with-copilot"], ["claude", "codex", "copilot"]),
        (["--target", "copilot", "--with-copilot"], ["copilot"]),
    ],
)
def test_init_targets_ignore_previous_copilot(options, expected, monkeypatch, capsys):
    from scripts import install

    monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
    monkeypatch.setattr(install, "_load_state", lambda: {"targets": {"global": {"copilot": {"profile": "old"}}}})
    calls = []
    monkeypatch.setattr(install, "cmd_init_unified", lambda args: calls.append(args.install_target))
    from scripts import herdr_setup

    monkeypatch.setattr(herdr_setup, "init_step", lambda *args: print("herdr detail"))
    monkeypatch.setattr(sys, "argv", ["agentihooks", "init", "--profile", "default", *options])
    install.main()
    assert calls == expected
    assert "herdr detail" not in capsys.readouterr().out


@pytest.mark.parametrize("verbose", [False, True])
def test_reporter_output(verbose, tmp_path, capsys):
    from scripts.init_reporter import report_target

    adapter = Mock()
    adapter.name = "codex"
    adapter.home.return_value = tmp_path
    adapter.summary_counts.return_value = {"mcp": 2, "skills": 3, "plugins": 0}
    record = {"profile": "engineer,brain", "profile_chain": ["anton-base", "engineer", "brain"]}

    def install_target():
        print("linked skill detail")
        print("  [WARN] missing optional command\n       Install the command to enable it.")
        print("stderr detail", file=sys.stderr)
        print("ERROR: operator action required", file=sys.stderr)

    report_target(install_target, adapter, lambda: record, verbose=verbose)
    output = capsys.readouterr()
    assert "[WARN] missing optional command" in output.out
    assert "ERROR: operator action required" in output.err
    assert "Install the command to enable it." in output.out
    assert ("stderr detail" in output.err) is verbose
    assert ("linked skill detail" in output.out) is verbose
    summaries = [line for line in output.out.splitlines() if line.startswith("Codex ")]
    assert summaries == [f"Codex {tmp_path} | 2 MCP servers, 3 skills, 0 plugins"]
    if not verbose:
        assert len(output.out.splitlines()) == 5
        assert "=" not in output.out
        assert "Profile engineer,brain | Chain anton-base, engineer, brain | Overlays none" in output.out


@pytest.mark.parametrize("verbose", [False, True])
def test_init_verbose_controls_all_details(verbose, monkeypatch, capsys):
    from scripts import herdr_setup, install

    monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
    monkeypatch.setattr(install, "_load_state", lambda: {})
    monkeypatch.setattr(install, "cmd_init_unified", lambda args: print("linked skill detail"))
    monkeypatch.setattr(herdr_setup, "init_step", lambda *args: print("herdr detail"))
    options = ["--verbose"] if verbose else []
    monkeypatch.setattr(sys, "argv", ["agentihooks", "init", "--profile", "default", *options])
    install.main()
    output = capsys.readouterr().out
    assert ("linked skill detail" in output) is verbose
    assert ("herdr detail" in output) is verbose
    assert sum(line.startswith(("Claude ", "Codex ")) for line in output.splitlines()) == 2
    if not verbose:
        assert len(output.splitlines()) == 5
        assert output.splitlines()[0] == "AgentiHooks installed"
        assert output.splitlines()[1] == "Bundle none"
        assert "=" not in output


@pytest.mark.parametrize("stored_profile", ["engineer", "package:engineer", "engineer,brain", "anton,brain"])
def test_codex_reuses_operator_profile(stored_profile, monkeypatch):
    from argparse import Namespace

    from scripts import herdr_setup, install
    from scripts.init_reporter import run_init

    state = {
        "targets": {
            "global": {
                "claude": {"profile": "anton,brain"},
                "codex": {"profile": stored_profile},
            }
        }
    }
    monkeypatch.setattr(install, "_load_state", lambda: state)
    monkeypatch.setattr(install, "_get_bundle_path", lambda: None)
    monkeypatch.setattr(
        install, "_resolve_profile_chain", lambda profile: [(name, None) for name in profile.split(",")]
    )
    calls = []
    monkeypatch.setattr(install, "cmd_init_unified", lambda args: calls.append((args.install_target, args.profile)))
    monkeypatch.setattr(herdr_setup, "init_step", lambda *args: None)
    args = Namespace(
        init_profile=None,
        init_settings_profile=None,
        init_target=None,
        with_copilot=False,
        verbose=False,
        init_herdr=None,
    )
    run_init(args, install)
    assert calls == [("claude", "anton,brain"), ("codex", "anton,brain")]


@pytest.mark.parametrize("verbose", [False, True])
def test_kept_mcp_notices_are_verbose_and_unique(verbose, capsys):
    from scripts.init_reporter import capture_details

    seen = set()
    for _ in range(2):
        with capture_details(verbose, seen):
            print("  [!!] Kept your existing MCP server(s), not overwritten: custom")
            print("       Use force to replace it.")
            print("Codex status line note")
    output = capsys.readouterr().out
    assert output.count("Kept your existing MCP server(s)") == int(verbose)
    assert output.count("Use force to replace it.") == int(verbose)
    assert ("Codex status line note" in output) is verbose


@pytest.mark.parametrize("target", ["claude", "codex", "copilot"])
def test_previous_install_rejects_role_only_state(target):
    from scripts import install
    from scripts.init_reporter import previous_install

    state = {"targets": {"global": {target: {"profile": "engineer,brain"}}}}
    assert previous_install(install, state, target) == {}


@pytest.mark.parametrize(
    ("text", "tty", "verbose", "expected"),
    [
        ("detail", False, False, ""),
        ("detail", False, True, "detail"),
        ("Profile: ", True, False, "Profile: "),
        ("\x1b[31mERROR: required\x1b[0m", False, False, "\x1b[31mERROR: required\x1b[0m"),
        ("detail\nERROR: required\nremediation\n", False, False, "ERROR: required remediation\n"),
        ("\x1b[31mERROR: required\x1b[0m\n", False, False, "\x1b[31mERROR: required\x1b[0m\n"),
    ],
)
def test_output_fragments(text, tty, verbose, expected, monkeypatch):
    from io import StringIO

    from scripts.init_reporter import InstallOutput

    stream = StringIO()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty)
    output = InstallOutput(stream, verbose)
    assert output.write(text) == len(text)
    output.flush()
    assert stream.getvalue() == expected
    output.flush()
    assert stream.getvalue() == expected
    output.write("detail\n")
    output.flush()


def test_output_defaults_and_separate_diagnostics():
    from io import StringIO

    from scripts.init_reporter import InstallOutput

    stream = StringIO()
    output = InstallOutput(stream)
    output.write("detail\n")
    output.write("ERROR: first\n")
    output.write("detail after notice\n")
    output.write("ERROR: second\n")
    output.flush()
    assert stream.getvalue() == "ERROR: first\nERROR: second\n"


@pytest.mark.parametrize(
    "record,profile_line",
    [
        ({}, "Profile default | Chain default | Overlays none"),
        (
            {"profile": "anton,brain", "settings_profile": "remote"},
            "Profile anton,brain | Chain anton, brain | Overlays remote",
        ),
    ],
)
def test_reporter_metadata_defaults_and_overlays(record, profile_line, tmp_path, capsys):
    from scripts.init_reporter import report_target

    adapter = Mock(name="adapter")
    adapter.name = "claude"
    adapter.home.return_value = tmp_path
    adapter.summary_counts.return_value = {"mcp": 1, "skills": 2, "plugins": 3}
    report_target(lambda: print("detail"), adapter, lambda: dict(record, bundle=tmp_path))
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "AgentiHooks installed",
        f"Bundle {tmp_path}",
        profile_line,
        f"Claude {tmp_path} | 1 MCP servers, 2 skills, 3 plugins",
    ]


def test_init_help_describes_defaults_and_flags(monkeypatch, capsys):
    from scripts import install

    monkeypatch.setattr(sys, "argv", ["agentihooks", "init", "--help"])
    with pytest.raises(SystemExit) as stopped:
        install.main()
    assert stopped.value.code == 0
    output = " ".join(capsys.readouterr().out.split())
    assert " Install only one agent CLI (claude | codex | copilot). Defaults to Claude and Codex. " in f" {output} "
    assert " Include Copilot in this install " in f" {output} "
    assert " Show full installation details " in f" {output} "


def test_init_command_forwards_options_once_and_deduplicates_notices(monkeypatch, tmp_path, capsys):
    from scripts import herdr_setup, install

    state = {}
    calls = []

    def install_target(args):
        calls.append(
            (args.install_target, args.profile, args.settings_profile, args.force, args.bundle, args.link_profile)
        )
        install._global_record(state, args.install_target, create=True).update(
            profile=args.profile, settings_profile=args.settings_profile
        )
        print("  [!!] Kept your existing MCP server(s), not overwritten: custom")

    monkeypatch.setattr(install, "_load_state", lambda: state)
    monkeypatch.setattr(install, "_get_bundle_path", lambda: tmp_path)
    monkeypatch.setattr(install, "_resolve_profile_chain", lambda name: [("base", None), (name, None)])
    monkeypatch.setattr(install, "cmd_init_unified", install_target)
    step = Mock()
    monkeypatch.setattr(herdr_setup, "init_step", step)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agentihooks",
            "init",
            "--profile",
            "default",
            "--settings-profile",
            "tune",
            "--bundle",
            str(tmp_path),
            "--link-profile",
            "extra=source",
            "--force",
            "--herdr",
            "no",
            "-v",
            "--with-copilot",
        ],
    )
    install.main()
    assert calls == [
        ("claude", "default", "tune", True, str(tmp_path), ["extra=source"]),
        ("codex", "default", "tune", False, None, []),
        ("copilot", "default", "tune", False, None, []),
    ]
    step.assert_called_once_with("no", True)
    output = capsys.readouterr().out
    assert output.count("Kept your existing MCP server(s)") == 1
    assert f"Bundle {tmp_path}" in output
    assert "Profile default | Chain base, default | Overlays tune" in output


def test_explicit_codex_reads_its_own_profile_and_report(monkeypatch, tmp_path, capsys):
    from scripts import herdr_setup, install

    state = {
        "targets": {
            "global": {
                "claude": {"profile": "default"},
                "codex": {"profile": "custom"},
            }
        }
    }
    calls = []
    monkeypatch.setattr(install, "_load_state", lambda: state)
    monkeypatch.setattr(install, "_get_bundle_path", lambda: tmp_path)
    monkeypatch.setattr(install, "_resolve_profile_chain", lambda name: [("base", None), (name, None)])
    monkeypatch.setattr(install, "cmd_init_unified", lambda args: calls.append((args.profile, args.settings_profile)))
    monkeypatch.setattr(herdr_setup, "init_step", lambda flag, interactive: None)
    monkeypatch.setattr(sys, "argv", ["agentihooks", "init", "--target", "codex"])
    install.main()
    assert calls == [("custom", "")]
    assert "Profile custom | Chain base, custom | Overlays none" in capsys.readouterr().out


def test_shared_diagnostics_ignore_color(monkeypatch, capsys):
    from scripts.init_reporter import capture_details

    seen = set()
    with capture_details(False, seen):
        print("\x1b[31mERROR: required\x1b[0m")
    with capture_details(False, seen):
        print("ERROR: required")
    assert capsys.readouterr().out == "\x1b[31mERROR: required\x1b[0m\n"


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("codex_profile, expected", [("custom", "custom"), ("engineer,brain", "anton,brain")])
def test_unified_init_recalls_the_safe_target_record(force, codex_profile, expected, monkeypatch):
    from argparse import Namespace

    from scripts import install

    state = {
        "targets": {
            "global": {
                "claude": {"profile": "anton,brain", "settings_profile": "operator"},
                "codex": {"profile": codex_profile, "settings_profile": "custom-overlay"},
            }
        }
    }
    monkeypatch.setattr(install, "_load_state", lambda: state)
    monkeypatch.setattr(install, "_get_bundle_path", lambda: None)
    monkeypatch.setattr(install, "_clean_state_dir", lambda: None)
    monkeypatch.setattr(install, "_save_state", lambda state: None)
    monkeypatch.setattr(install, "_rerender_profile_homes", lambda target: None)
    monkeypatch.setattr(install, "_update_bashrc_block", lambda: None)
    installed = Mock()
    monkeypatch.setattr(install, "install_global", installed)
    args = Namespace(
        profile=None, install_target="codex", force=force, bundle=None, no_discover=True, settings_profile=None
    )
    install.cmd_init_unified(args)
    result = installed.call_args.args[0]
    assert result.profile == expected
    assert result.install_target == "codex"
    assert result.settings_profile == ("custom-overlay" if codex_profile == "custom" else "operator")


def test_incomplete_target_record_borrows_operator_profile():
    from scripts import install
    from scripts.init_reporter import previous_install

    state = {"targets": {"global": {"claude": {"profile": "anton"}, "codex": {"path": "home"}}}}
    assert previous_install(install, state, "codex") == {"profile": "anton"}


def test_reporter_preserves_failures(capsys):
    from scripts.init_reporter import report_target

    def fail():
        print("ERROR: install refused\nRun from the operator shell.", file=sys.stderr)
        raise SystemExit(2)

    with pytest.raises(SystemExit, match="2"):
        report_target(fail, Mock(), lambda: {})
    assert capsys.readouterr().err == "ERROR: install refused Run from the operator shell.\n"


@pytest.mark.parametrize("target", ["claude", "codex", "copilot"])
def test_adapter_summary_counts_installed_artifacts(target, tmp_path, monkeypatch):
    from scripts import install
    from scripts.targets import claude_target, codex_target, copilot_target, get_adapter

    home = tmp_path / target
    home.mkdir()
    skills = home / "skills"
    skills.mkdir()
    for name in ("one", "two"):
        skill = skills / name
        skill.mkdir()
        (skill / "SKILL.md").write_text("skill")
    (skills / "empty").mkdir()
    (skills / "unrelated.txt").write_text("not a skill")
    monkeypatch.setattr(install, "CLAUDE_HOME", home)
    monkeypatch.setattr(claude_target, "_install_module", lambda: install)
    monkeypatch.setattr(codex_target, "codex_home", lambda: home)
    monkeypatch.setattr(codex_target, "agents_skills_home", lambda: skills)
    monkeypatch.setattr(copilot_target, "copilot_home", lambda: home)
    monkeypatch.setattr(copilot_target, "agents_skills_home", lambda: skills)
    mcp_file = tmp_path / "claude.json"
    monkeypatch.setattr(install, "_CLAUDE_JSON", mcp_file)
    mcp_file.write_text(json.dumps({"mcpServers": {"one": {}, "two": {}}}))
    (home / "settings.json").write_text(json.dumps({"enabledPlugins": {"on": True, "off": False}}))
    (home / "config.toml").write_text('[mcp_servers.one]\ncommand = "one"\n[mcp_servers.two]\ncommand = "two"\n')
    (home / "mcp-config.json").write_text(json.dumps({"mcpServers": {"one": {}, "two": {}}}))
    assert get_adapter(target).summary_counts() == {"mcp": 2, "skills": 2, "plugins": 1 if target == "claude" else 0}
