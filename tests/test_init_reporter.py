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
    summaries = [line for line in output.out.splitlines() if line.startswith("codex ")]
    assert summaries == [
        f"codex {tmp_path} | profile=engineer,brain | chain=anton-base,engineer,brain | mcp=2 skills=3 plugins=0"
    ]
    if not verbose:
        assert len(output.out.splitlines()) == 2


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
    assert sum(line.startswith(("claude ", "codex ")) for line in output.splitlines()) == 2
    if not verbose:
        assert len(output.splitlines()) == 2


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
