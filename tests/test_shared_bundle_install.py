import argparse
import json
import tomllib
from pathlib import Path

import pytest

from scripts import install
from scripts.profiles import bundles, render
from tests.test_user_bundle import _init
from tests.test_user_bundle_install import (  # noqa: F401
    _dep,
    _deps,
    _install,
    _isolate_stamp,
    _manifesto,
    _settings,
    _skill,
    _write,
    clean_manifesto_env,
    real_root_manifest,
    world,
)

PERSONAS = [("codex", "AGENTS.md"), ("copilot", "copilot-instructions.md")]


@pytest.fixture
def world3(world):  # noqa: F811
    shared = world["root"].parent / "shared"
    _write(shared / "profiles" / ".keep", "")
    return {**world, "shared": shared}


def _link_all(world3) -> None:
    bundles.link(world3["root"])
    bundles.link(world3["user"], user=True)
    bundles.link(world3["shared"], shared=True)


def test_settings_layer_root_then_shared_then_user_and_the_profile_wins_over_all(world3):
    for name, env in (
        ("root", {"WHO": "root", "K1": "root", "K2": "root", "K3": "root"}),
        ("shared", {"WHO": "shared", "K2": "shared", "K3": "shared", "TEAM": "1"}),
        ("user", {"WHO": "user", "K3": "user"}),
    ):
        _write(world3[name] / ".claude" / "settings.overrides.json", json.dumps({"env": env}))
    _link_all(world3)

    _install()

    env = _settings(world3)["env"]
    assert (env["K1"], env["K2"], env["K3"], env["TEAM"]) == ("root", "shared", "user", "1")
    assert env["WHO"] == "profile"


def test_hooks_append_root_then_shared_then_user_once_each_across_reinstalls(world3):
    def hook(command: str) -> dict:
        return {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}}

    for name in ("root", "shared", "user"):
        _write(world3[name] / ".claude" / "settings.overrides.json", json.dumps(hook(f"echo {name}-hook")))
    _link_all(world3)

    _install()
    _install()

    commands = [h["command"] for group in _settings(world3)["hooks"]["Stop"] for h in group["hooks"]]
    assert [commands.count(f"echo {name}-hook") for name in ("root", "shared", "user")] == [1, 1, 1]
    assert commands.index("echo root-hook") < commands.index("echo shared-hook") < commands.index("echo user-hook")


def test_skills_link_from_all_three_and_the_later_bundle_wins_each_clash(world3):
    _skill(world3["root"], "only-root", "root")
    _skill(world3["root"], "clash-rs", "root")
    _skill(world3["shared"], "only-shared", "shared")
    _skill(world3["shared"], "clash-rs", "shared")
    _skill(world3["shared"], "clash-su", "shared")
    _skill(world3["user"], "clash-su", "user")
    _link_all(world3)

    _install()

    skills = world3["home"] / ".claude" / "skills"

    def source(name: str, bundle: str) -> Path:
        return world3[bundle] / ".claude" / "skills" / name

    assert (skills / "only-root").resolve() == source("only-root", "root")
    assert (skills / "only-shared").resolve() == source("only-shared", "shared")
    assert (skills / "clash-rs").resolve() == source("clash-rs", "shared")
    assert (skills / "clash-su").resolve() == source("clash-su", "user")


def test_rules_copy_from_all_three_and_the_later_bundle_wins_each_clash(world3):
    for name in ("root", "shared", "user"):
        _write(world3[name] / ".claude" / "rules" / f"only-{name}.md", f"{name}\n")
    _write(world3["root"] / ".claude" / "rules" / "clash.md", "ROOT\n")
    _write(world3["shared"] / ".claude" / "rules" / "clash.md", "SHARED\n")
    _link_all(world3)

    _install()

    rules = world3["home"] / ".claude" / "rules"
    assert [(rules / f"only-{name}.md").read_text() for name in ("root", "shared", "user")] == [
        "root\n",
        "shared\n",
        "user\n",
    ]
    assert (rules / "clash.md").read_text() == "SHARED\n"


def test_the_three_directive_sources_land_once_in_order_ahead_of_the_profile_and_reinstall_is_stable(world3):
    for name in ("root", "shared", "user"):
        _write(world3[name] / ".claude" / "CLAUDE.md", f"{name.upper()} DIRECTIVE\n")
    _link_all(world3)

    _install()
    first = (world3["home"] / ".claude" / "CLAUDE.md").read_text()
    _install()
    text = (world3["home"] / ".claude" / "CLAUDE.md").read_text()

    assert text == first
    assert [text.count(f"{name} DIRECTIVE") for name in ("ROOT", "SHARED", "USER")] == [1, 1, 1]
    assert text.count(install._BUNDLE_CLAUDE_MD_BEGIN) == 1
    assert text.index("ROOT DIRECTIVE") < text.index("SHARED DIRECTIVE") < text.index("USER DIRECTIVE")
    assert text.index("USER DIRECTIVE") < text.index("tiny persona")


def test_a_shared_directive_alone_still_prepends(world3):
    _write(world3["shared"] / ".claude" / "CLAUDE.md", "SHARED ONLY\n")
    bundles.link(world3["shared"], shared=True)

    _install()

    text = (world3["home"] / ".claude" / "CLAUDE.md").read_text()
    assert text.startswith(install._BUNDLE_CLAUDE_MD_BEGIN)
    assert text.index("SHARED ONLY") < text.index("tiny persona")


@pytest.mark.parametrize(("target", "persona"), PERSONAS)
def test_compiled_personas_carry_the_three_directive_sources_once_in_order(world3, target, persona):
    for name in ("root", "shared", "user"):
        _write(world3[name] / ".claude" / "CLAUDE.md", f"{name.upper()} DIRECTIVE\n")
    _link_all(world3)

    _install(target)

    text = (world3["home"] / f".{target}" / persona).read_text()
    assert [text.count(f"{name} DIRECTIVE") for name in ("ROOT", "SHARED", "USER")] == [1, 1, 1]
    assert text.index("ROOT DIRECTIVE") < text.index("SHARED DIRECTIVE") < text.index("USER DIRECTIVE")
    assert text.index("USER DIRECTIVE") < text.index("tiny persona")


def test_codex_applies_the_three_config_layers_in_order(world3):
    _write(world3["root"] / ".codex" / "config.overrides.toml", 'model = "root-model"\nsandbox_mode = "read-only"\n')
    _write(world3["shared"] / ".codex" / "config.overrides.toml", 'model = "shared-model"\nweb_search = "live"\n')
    _write(world3["user"] / ".codex" / "config.overrides.toml", 'model = "user-model"\n')
    _link_all(world3)

    _install("codex")

    config = tomllib.loads((world3["home"] / ".codex" / "config.toml").read_text())
    assert config["model"] == "user-model"
    assert config["sandbox_mode"] == "read-only"
    assert config["web_search"] == "live"


def test_mcp_servers_register_from_all_three_and_the_later_bundle_wins_each_clash(world3):
    def servers(**commands: str) -> str:
        return json.dumps({"mcpServers": {name: {"command": command} for name, command in commands.items()}})

    _write(world3["root"] / ".claude" / ".mcp.json", servers(onlyroot="r", clash="root-clash"))
    _write(world3["shared"] / ".claude" / ".mcp.json", servers(onlyshared="s", clash="shared-clash", other="s-other"))
    _write(world3["user"] / ".claude" / ".mcp.json", servers(onlyuser="u", other="user-other"))
    _link_all(world3)

    _install()

    found = json.loads((world3["home"] / ".claude.json").read_text())["mcpServers"]
    assert {name: found[name]["command"] for name in ("onlyroot", "onlyshared", "onlyuser", "clash", "other")} == {
        "onlyroot": "r",
        "onlyshared": "s",
        "onlyuser": "u",
        "clash": "shared-clash",
        "other": "user-other",
    }
    assert {"onlyshared", "clash", "other"} <= set(install._collect_all_managed_mcp_servers())


def test_unlinking_the_shared_bundle_removes_its_generated_links_and_its_mcp_server_and_keeps_every_file(world3):
    _skill(world3["root"], "root-skill", "root")
    _skill(world3["shared"], "team-skill", "shared")
    _write(world3["shared"] / ".claude" / ".mcp.json", json.dumps({"mcpServers": {"team": {"command": "t"}}}))
    _link_all(world3)
    _install()
    link = world3["home"] / ".claude" / "skills" / "team-skill"
    assert link.is_symlink()

    bundles.unlink(shared=True)
    _install()

    assert not link.exists() and not link.is_symlink()
    assert (world3["home"] / ".claude" / "skills" / "root-skill").is_symlink()
    assert (world3["shared"] / ".claude" / "skills" / "team-skill" / "SKILL.md").is_file()
    assert "team" not in json.loads((world3["home"] / ".claude.json").read_text())["mcpServers"]


def test_a_machine_without_a_shared_bundle_installs_the_same_two_layers_as_before(world3):
    _write(world3["root"] / ".claude" / "settings.overrides.json", json.dumps({"env": {"ONLY": "root"}}))
    _write(world3["user"] / ".claude" / "settings.overrides.json", json.dumps({"env": {"ONLY": "user"}}))
    bundles.link(world3["root"])
    bundles.link(world3["user"], user=True)

    _install()

    assert _settings(world3)["env"]["ONLY"] == "user"


def test_the_render_stamp_records_the_shared_revision_between_root_and_user(world3, monkeypatch):
    import os
    import subprocess

    def commit(root: Path) -> str:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
            subprocess.run(["git", *args], cwd=root, env=env, check=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()

    _link_all(world3)
    commits = {name: commit(world3[name]) for name in ("root", "shared", "user")}
    _isolate_stamp(monkeypatch)

    stamp = render._stamp(world3["root"], [])

    assert stamp["bundle_commit"] == commits["root"]
    assert stamp["shared_bundle_commit"] == commits["shared"]
    assert stamp["user_bundle_commit"] == commits["user"]
    assert list(stamp)[:3] == ["bundle_commit", "shared_bundle_commit", "user_bundle_commit"]


def test_the_render_stamp_has_no_shared_field_without_a_shared_bundle(world3, monkeypatch):
    bundles.link(world3["root"])
    _isolate_stamp(monkeypatch)

    assert "shared_bundle_commit" not in render._stamp(world3["root"], [])


def test_rendering_refuses_a_linked_shared_bundle_whose_directory_is_gone(world3):
    bundles.link(world3["root"])
    bundles.link(world3["shared"], shared=True)
    world3["shared"].rename(world3["shared"].with_name("moved"))

    with pytest.raises(ValueError, match="linked shared bundle .* is missing"):
        render._shared()


def test_the_scratch_render_carries_the_registered_shared_bundle(world3, monkeypatch, tmp_path):
    seen = {}

    def fake_run(argv, env, cwd):
        seen["env"] = env
        return type("Done", (), {"returncode": 0})()

    _link_all(world3)
    monkeypatch.setattr(render.subprocess, "run", fake_run)
    args = argparse.Namespace(
        bundle=None,
        user_bundle=None,
        shared_bundle=None,
        out=tmp_path / "scratch",
        name="tiny",
        target="claude",
        overlay=[],
        force=False,
    )
    (tmp_path / "scratch").mkdir()

    assert render._render_scratch(args) == 0

    assert seen["env"]["AGENTIHOOKS_BUNDLE_PATH"] == str(world3["root"].resolve())
    assert seen["env"]["AGENTIHOOKS_SHARED_BUNDLE_PATH"] == str(world3["shared"].resolve())
    assert seen["env"]["AGENTIHOOKS_USER_BUNDLE_PATH"] == str(world3["user"].resolve())


def test_dependencies_combine_root_shared_user_and_the_later_entry_wins_a_clash(world3, real_root_manifest):  # noqa: F811
    from scripts import deps_preflight

    _deps(world3, "root", _dep("only-root"), _dep("clash", hint="root"))
    _deps(world3, "shared", _dep("only-shared"), _dep("clash", hint="shared"))
    _deps(world3, "user", _dep("only-user"), _dep("clash", hint="user"))
    _link_all(world3)

    found = {dep.id: dep for dep in deps_preflight.declared()}

    assert sorted(found) == ["clash", "only-root", "only-shared", "only-user"]
    assert found["clash"].hint == "user"
    assert deps_preflight.manifest_paths() == [world3[name] / "deps.json" for name in ("root", "shared", "user")]


def test_fleet_plugins_follow_the_same_precedence_across_three_bundles(world3):
    from scripts import deps_preflight

    plugin = {"kind": "claude-plugin", "check": ["true"], "install": ["true"]}
    _write(world3["shared"] / "deps.json", json.dumps({"deps": [{"id": "team-plugin", **plugin}]}))

    assert "team-plugin" in deps_preflight.fleet_plugins(world3["root"], world3["user"], shared=world3["shared"])
    assert "team-plugin" not in deps_preflight.fleet_plugins(world3["root"], world3["user"])


def test_manifestos_union_by_stem_the_later_bundle_wins_and_the_result_stays_sorted(world3, clean_manifesto_env):  # noqa: F811
    from hooks import config

    _manifesto(world3["root"], "core.md", "ROOT CORE")
    _manifesto(world3["root"], "base.md", "ROOT BASE")
    _manifesto(world3["shared"], "core.md", "SHARED CORE")
    _manifesto(world3["shared"], "team.md", "SHARED TEAM")
    _manifesto(world3["user"], "CORE.md", "USER CORE")
    _manifesto(world3["user"], "mine.md", "USER MINE")

    found = config._resolve_manifesto_paths(world3["root"], user_root=world3["user"], shared_root=world3["shared"])

    assert [Path(p).parent.parent.name + "/" + Path(p).name for p in found] == [
        "root/base.md",
        "user/CORE.md",
        "user/mine.md",
        "shared/team.md",
    ]


def test_a_root_and_user_manifesto_resolution_without_shared_is_unchanged(world3, clean_manifesto_env):  # noqa: F811
    from hooks import config

    _manifesto(world3["root"], "core.md", "ROOT CORE")
    _manifesto(world3["user"], "CORE.md", "USER CORE")

    found = config._resolve_manifesto_paths(world3["root"], user_root=world3["user"])

    assert [Path(p).parent.parent.name + "/" + Path(p).name for p in found] == ["user/CORE.md"]


def test_the_runtime_reader_and_the_installer_agree_on_the_three_manifestos(world3, clean_manifesto_env):  # noqa: F811
    from hooks.context import ci_manifesto
    from scripts.profiles import manifestos

    _manifesto(world3["root"], "core.md", "ROOT CORE")
    _manifesto(world3["shared"], "team.md", "SHARED TEAM")
    _manifesto(world3["user"], "mine.md", "USER MINE")
    _link_all(world3)

    installed = manifestos.paths(world3["root"], [], world3["user"], shared=world3["shared"])

    assert ci_manifesto._manifesto_paths() == installed
    assert [p.name for p in installed] == ["core.md", "mine.md", "team.md"]


def test_force_init_keeps_the_shared_link_through_a_real_install(world3, monkeypatch):
    _link_all(world3)
    monkeypatch.setattr(install, "_clean_state_dir", lambda: install.STATE_JSON.unlink())

    _init(monkeypatch, force=True)

    assert json.loads(install.STATE_JSON.read_text())["shared_bundle"]["path"] == str(world3["shared"])
