import argparse
import sys
from pathlib import Path

from scripts import install
from tests import test_install as install_tests

PACKAGED = install.PACKAGE_FEATURES_DIR / "agents" / "scouter.md"


def run_install(tmp_path, monkeypatch, target, bundle=None):
    profiles = install_tests.TestInstallGlobalHonoursTarget()._tiny_profile(tmp_path)
    monkeypatch.setattr(install, "PROFILES_DIR", profiles)
    monkeypatch.setattr(install, "_get_bundle_path", lambda: bundle)
    monkeypatch.setattr(install, "_install_cli_tool", lambda *a, **k: None)
    monkeypatch.setattr(install, "_ensure_mcp_daemon", lambda *a, **k: None)
    monkeypatch.setattr(install, "_resolve_hooks_python", lambda *a, **k: Path(sys.executable))
    install.install_global(argparse.Namespace(profile="tiny", settings_profile="", install_target=target))
    return Path.home()


def test_claude_install_links_the_packaged_scouter(tmp_path, monkeypatch, capsys):
    home = run_install(tmp_path, monkeypatch, "claude")
    link = home / ".claude" / "agents" / "scouter.md"
    assert link.is_symlink()
    assert link.resolve() == PACKAGED.resolve()
    assert f"  [OK] Linked agent 'scouter.md' → {PACKAGED}" in capsys.readouterr().out


def test_scouter_is_a_read_only_sonnet_agent():
    head = PACKAGED.read_text().split("---")[1]
    assert "model: sonnet\n" in head
    assert "tools:\n  - WebSearch\n  - WebFetch\n  - Read\n  - Grep\n  - Glob\n" in head


def test_bundle_agent_with_the_same_name_overrides_the_scouter(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    override = bundle / ".claude" / "agents" / "scouter.md"
    override.parent.mkdir(parents=True)
    override.write_text("# bundle scouter\n")
    home = run_install(tmp_path, monkeypatch, "claude", bundle=bundle)
    assert (home / ".claude" / "agents" / "scouter.md").resolve() == override.resolve()


def test_codex_install_skips_agents_with_its_message(tmp_path, monkeypatch, capsys):
    home = run_install(tmp_path, monkeypatch, "codex")
    out = capsys.readouterr().out
    assert (
        "  [--] Codex has no custom-subagent registry — agents skipped (docs/reference/CODEX-COMPAT.md §3 row 16)."
        in out
    )
    assert sorted(p.name for p in home.rglob("scouter.md")) == []


def test_copilot_install_translates_the_scouter_with_read_only_tools(tmp_path, monkeypatch):
    import yaml

    from scripts.targets.copilot_target import copilot_home

    run_install(tmp_path, monkeypatch, "copilot")
    head = yaml.safe_load((copilot_home() / "agents" / "scouter.md").read_text().split("---")[1])
    assert head["name"] == "scouter"
    assert head["tools"] == ["glob", "grep", "view", "web_fetch", "web_search"]
