import argparse
import json
import sys
import tomllib
from pathlib import Path

import install as legacy_install
import pytest

from scripts import install
from scripts.profiles import bundles, render, sources

INSTALLERS = (install, legacy_install)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _skill(root: Path, name: str, body: str) -> None:
    _write(root / ".claude" / "skills" / name / "SKILL.md", f"---\nname: {name}\n---\n{body}\n")


@pytest.fixture
def world(tmp_path, monkeypatch):
    profiles = tmp_path / "builtin"
    tiny = profiles / "tiny"
    _write(tiny / "CLAUDE.md", "tiny persona\n")
    _write(tiny / ".claude" / "settings.overrides.json", json.dumps({"env": {"WHO": "profile"}}))
    base = profiles / "_base"
    _write(base / "settings.base.json", json.dumps({"hooks": {}}))
    _write(base / "settings.base.copilot.json", json.dumps({"disableAllHooks": False}))
    _write(base / "config.base.toml", 'approval_policy = "on-request"\n')
    root = tmp_path / "root"
    user = tmp_path / "user"
    for bundle in (root, user):
        _write(bundle / "profiles" / ".keep", "")
    for module in INSTALLERS:
        monkeypatch.setattr(module, "PROFILES_DIR", profiles, raising=False)
        monkeypatch.setattr(module, "_install_cli_tool", lambda *a, **k: None, raising=False)
        monkeypatch.setattr(module, "_ensure_mcp_daemon", lambda *a, **k: None, raising=False)
        monkeypatch.setattr(module, "_resolve_hooks_python", lambda *a, **k: Path(sys.executable), raising=False)
    return {"root": root, "user": user, "home": Path.home()}


def _install(target: str = "claude") -> None:
    install.install_global(argparse.Namespace(profile="tiny", settings_profile="", install_target=target))


def _link_both(world) -> None:
    bundles.link(world["root"])
    bundles.link(world["user"], user=True)


def _settings(world) -> dict:
    return json.loads((world["home"] / ".claude" / "settings.json").read_text())


def test_user_settings_override_root_and_the_profile_wins_over_both(world):
    _write(world["root"] / ".claude" / "settings.overrides.json", json.dumps({"env": {"WHO": "root", "ROOT": "1"}}))
    _write(world["user"] / ".claude" / "settings.overrides.json", json.dumps({"env": {"WHO": "user", "USER": "1"}}))
    _link_both(world)

    _install()

    env = _settings(world)["env"]
    assert env["ROOT"] == "1"
    assert env["USER"] == "1"
    assert env["WHO"] == "profile"


def test_user_settings_override_root_when_no_profile_sets_the_key(world):
    _write(world["root"] / ".claude" / "settings.overrides.json", json.dumps({"env": {"ONLY": "root"}}))
    _write(world["user"] / ".claude" / "settings.overrides.json", json.dumps({"env": {"ONLY": "user"}}))
    _link_both(world)

    _install()

    assert _settings(world)["env"]["ONLY"] == "user"


def test_root_then_user_hooks_append_in_order_and_do_not_duplicate_on_reinstall(world):
    def hook(command: str) -> dict:
        return {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}}

    _write(world["root"] / ".claude" / "settings.overrides.json", json.dumps(hook("echo root-hook")))
    _write(world["user"] / ".claude" / "settings.overrides.json", json.dumps(hook("echo user-hook")))
    _link_both(world)

    _install()
    _install()

    commands = [h["command"] for group in _settings(world)["hooks"]["Stop"] for h in group["hooks"]]
    assert commands.count("echo root-hook") == 1
    assert commands.count("echo user-hook") == 1
    assert commands.index("echo root-hook") < commands.index("echo user-hook")


def test_a_foreign_hook_survives_installing_with_a_user_bundle(world):
    foreign = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo not-ours"}]}]}}
    _write(world["home"] / ".claude" / "settings.json", json.dumps(foreign))
    _write(
        world["user"] / ".claude" / "settings.overrides.json",
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo user-hook"}]}]}}),
    )
    _link_both(world)

    _install()

    commands = [h["command"] for group in _settings(world)["hooks"]["Stop"] for h in group["hooks"]]
    assert "echo not-ours" in commands
    assert "echo user-hook" in commands


def test_shared_and_personal_skills_link_and_the_user_skill_wins_a_name_clash(world):
    _skill(world["root"], "shared-skill", "root")
    _skill(world["root"], "clash", "root")
    _skill(world["user"], "personal-skill", "user")
    _skill(world["user"], "clash", "user")
    _link_both(world)

    _install()

    skills = world["home"] / ".claude" / "skills"
    assert (skills / "shared-skill").resolve() == world["root"] / ".claude" / "skills" / "shared-skill"
    assert (skills / "personal-skill").resolve() == world["user"] / ".claude" / "skills" / "personal-skill"
    assert (skills / "clash").resolve() == world["user"] / ".claude" / "skills" / "clash"


def test_root_and_user_rules_are_copied_and_the_user_rule_wins_a_name_clash(world):
    _write(world["root"] / ".claude" / "rules" / "shared.md", "ROOT RULE\n")
    _write(world["root"] / ".claude" / "rules" / "clash.md", "ROOT CLASH\n")
    _write(world["user"] / ".claude" / "rules" / "mine.md", "USER RULE\n")
    _write(world["user"] / ".claude" / "rules" / "clash.md", "USER CLASH\n")
    _link_both(world)

    _install()

    rules = world["home"] / ".claude" / "rules"
    assert (rules / "shared.md").read_text() == "ROOT RULE\n"
    assert (rules / "mine.md").read_text() == "USER RULE\n"
    assert (rules / "clash.md").read_text() == "USER CLASH\n"


def test_both_shared_directive_sources_land_once_ahead_of_the_profile_and_reinstall_is_stable(world):
    _write(world["root"] / ".claude" / "CLAUDE.md", "ROOT DIRECTIVE\n")
    _write(world["user"] / ".claude" / "CLAUDE.md", "USER DIRECTIVE\n")
    _link_both(world)

    _install()
    first = (world["home"] / ".claude" / "CLAUDE.md").read_text()
    _install()
    text = (world["home"] / ".claude" / "CLAUDE.md").read_text()

    assert text == first
    assert text.count("ROOT DIRECTIVE") == 1
    assert text.count("USER DIRECTIVE") == 1
    assert text.count(install._BUNDLE_CLAUDE_MD_BEGIN) == 1
    assert text.index("ROOT DIRECTIVE") < text.index("USER DIRECTIVE") < text.index("tiny persona")


def test_a_user_directive_alone_still_prepends(world):
    _write(world["user"] / ".claude" / "CLAUDE.md", "USER ONLY\n")
    bundles.link(world["user"], user=True)

    _install()

    text = (world["home"] / ".claude" / "CLAUDE.md").read_text()
    assert text.startswith(install._BUNDLE_CLAUDE_MD_BEGIN)
    assert text.index("USER ONLY") < text.index("tiny persona")


@pytest.mark.parametrize(("target", "persona"), [("codex", "AGENTS.md"), ("copilot", "copilot-instructions.md")])
def test_compiled_personas_carry_both_directive_sources_once(world, target, persona):
    _write(world["root"] / ".claude" / "CLAUDE.md", "ROOT DIRECTIVE\n")
    _write(world["user"] / ".claude" / "CLAUDE.md", "USER DIRECTIVE\n")
    _link_both(world)

    _install(target)

    text = (world["home"] / f".{target}" / persona).read_text()
    assert text.count("ROOT DIRECTIVE") == 1
    assert text.count("USER DIRECTIVE") == 1
    assert text.index("ROOT DIRECTIVE") < text.index("USER DIRECTIVE") < text.index("tiny persona")


def test_codex_applies_both_bundle_config_layers(world):
    _write(world["root"] / ".codex" / "config.overrides.toml", 'model = "root-model"\nsandbox_mode = "read-only"\n')
    _write(world["user"] / ".codex" / "config.overrides.toml", 'model = "user-model"\n')
    _link_both(world)

    _install("codex")

    config = tomllib.loads((world["home"] / ".codex" / "config.toml").read_text())
    assert config["model"] == "user-model"
    assert config["sandbox_mode"] == "read-only"


def test_mcp_servers_from_both_bundles_register_and_the_user_server_wins_a_name_clash(world):
    _write(
        world["root"] / ".claude" / ".mcp.json",
        json.dumps({"mcpServers": {"shared": {"command": "root-cmd"}, "clash": {"command": "root-clash"}}}),
    )
    _write(
        world["user"] / ".claude" / ".mcp.json",
        json.dumps({"mcpServers": {"mine": {"command": "user-cmd"}, "clash": {"command": "user-clash"}}}),
    )
    _link_both(world)

    _install()

    servers = json.loads((world["home"] / ".claude.json").read_text())["mcpServers"]
    assert servers["shared"]["command"] == "root-cmd"
    assert servers["mine"]["command"] == "user-cmd"
    assert servers["clash"]["command"] == "user-clash"
    assert {"shared", "mine", "clash"} <= set(install._collect_all_managed_mcp_servers())


def test_dropping_the_user_bundle_removes_its_mcp_server_and_keeps_the_root_one(world):
    _write(world["root"] / ".claude" / ".mcp.json", json.dumps({"mcpServers": {"shared": {"command": "root-cmd"}}}))
    _write(world["user"] / ".claude" / ".mcp.json", json.dumps({"mcpServers": {"mine": {"command": "user-cmd"}}}))
    _link_both(world)
    _install()

    bundles.unlink(user=True)
    _install()

    servers = json.loads((world["home"] / ".claude.json").read_text())["mcpServers"]
    assert "shared" in servers
    assert "mine" not in servers


def test_unlinking_the_user_bundle_removes_its_generated_links_and_leaves_every_source_file(world):
    _skill(world["root"], "shared-skill", "root")
    _skill(world["user"], "personal-skill", "user")
    _write(world["user"] / ".claude" / "rules" / "mine.md", "USER RULE\n")
    _link_both(world)
    _install()
    skills = world["home"] / ".claude" / "skills"
    rule = world["home"] / ".claude" / "rules" / "mine.md"
    assert (skills / "personal-skill").is_symlink()
    assert rule.is_file()

    bundles.unlink(user=True)

    assert not (skills / "personal-skill").exists() and not (skills / "personal-skill").is_symlink()
    assert not rule.exists()
    assert (skills / "shared-skill").is_symlink()
    assert (world["user"] / ".claude" / "skills" / "personal-skill" / "SKILL.md").is_file()
    assert (world["user"] / ".claude" / "rules" / "mine.md").read_text() == "USER RULE\n"
    assert (world["root"] / ".claude" / "skills" / "shared-skill" / "SKILL.md").is_file()
    assert json.loads(install.STATE_JSON.read_text())["bundle"]["path"] == str(world["root"])


def test_unlinking_the_user_bundle_drops_its_directive_block_on_the_next_install(world):
    _write(world["root"] / ".claude" / "CLAUDE.md", "ROOT DIRECTIVE\n")
    _write(world["user"] / ".claude" / "CLAUDE.md", "USER DIRECTIVE\n")
    _link_both(world)
    _install()

    bundles.unlink(user=True)
    _install()

    text = (world["home"] / ".claude" / "CLAUDE.md").read_text()
    assert "ROOT DIRECTIVE" in text
    assert "USER DIRECTIVE" not in text


@pytest.mark.parametrize("target", ["claude", "codex", "copilot"])
def test_a_machine_with_no_user_bundle_installs_exactly_as_before(world, target):
    _skill(world["root"], "shared-skill", "root")
    _write(world["root"] / ".claude" / "CLAUDE.md", "ROOT DIRECTIVE\n")
    bundles.link(world["root"])

    _install(target)

    home = world["home"]
    persona = {
        "claude": ".claude/CLAUDE.md",
        "codex": ".codex/AGENTS.md",
        "copilot": ".copilot/copilot-instructions.md",
    }
    text = (home / persona[target]).read_text()
    assert text.count("ROOT DIRECTIVE") == 1
    assert "user bundle" not in text.lower()
    assert "user_bundle" not in json.loads(install.STATE_JSON.read_text())


def test_skill_link_repair_resolves_a_dangling_link_to_the_user_bundle_copy(world):
    from scripts import skill_links

    _skill(world["user"], "personal-skill", "user")
    _link_both(world)
    _install()
    link = world["home"] / ".claude" / "skills" / "personal-skill"
    link.unlink()
    link.symlink_to(world["user"] / ".claude" / "skills" / "gone")
    install._state_record_links([(link, world["user"] / ".claude" / "skills" / "gone", "skills")])

    skill_links.repair()

    assert link.resolve() == world["user"] / ".claude" / "skills" / "personal-skill"


def _isolate_stamp(monkeypatch) -> None:
    monkeypatch.setattr(render, "_base_digest", lambda: "base")
    monkeypatch.setattr(render, "_settings", lambda *args, **kwargs: {})
    monkeypatch.setattr(render, "_plugins", lambda chain, enabled: {})


def test_the_render_stamp_records_both_source_revisions(world, monkeypatch):
    import subprocess

    def commit(root: Path) -> str:
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
            subprocess.run(["git", *args], cwd=root, env={**__import__("os").environ, **env}, check=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()

    _link_both(world)
    root_commit, user_commit = commit(world["root"]), commit(world["user"])
    _isolate_stamp(monkeypatch)

    stamp = render._stamp(world["root"], [])

    assert stamp["bundle_commit"] == root_commit
    assert stamp["user_bundle_commit"] == user_commit


def test_the_render_stamp_has_no_user_field_without_a_user_bundle(world, monkeypatch):
    bundles.link(world["root"])
    _isolate_stamp(monkeypatch)

    assert "user_bundle_commit" not in render._stamp(world["root"], [])


def test_rendering_refuses_a_linked_user_bundle_whose_directory_is_gone(world):
    bundles.link(world["root"])
    bundles.link(world["user"], user=True)
    world["user"].rename(world["user"].with_name("moved"))

    with pytest.raises(ValueError, match="linked user bundle .* is missing"):
        render._user()


def test_render_features_and_sources_include_the_user_bundle(world):
    _skill(world["root"], "shared-skill", "root")
    _skill(world["user"], "personal-skill", "user")
    _write(world["user"] / ".claude" / "CLAUDE.md", "USER DIRECTIVE\n")
    _link_both(world)

    found = render._features("skills", lambda item: True, world["root"], [])
    files = sources.doctrine_files(world["root"], [], world["user"])

    assert {"shared-skill", "personal-skill"} <= set(found)
    assert world["user"] / ".claude" / "CLAUDE.md" in files


def test_the_scratch_render_carries_the_registered_user_bundle(world, monkeypatch, tmp_path):
    seen = {}

    def fake_run(argv, env, cwd):
        seen["env"] = env
        return type("Done", (), {"returncode": 0})()

    _link_both(world)
    monkeypatch.setattr(render.subprocess, "run", fake_run)
    args = argparse.Namespace(
        bundle=None, out=tmp_path / "scratch", name="tiny", target="claude", overlay=[], force=False
    )
    (tmp_path / "scratch").mkdir()

    assert render._render_scratch(args) == 0

    assert seen["env"]["AGENTIHOOKS_BUNDLE_PATH"] == str(world["root"].resolve())
    assert seen["env"]["AGENTIHOOKS_USER_BUNDLE_PATH"] == str(world["user"].resolve())
