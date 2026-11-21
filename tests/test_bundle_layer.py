import argparse
import json
import re
import shutil
import tomllib

import pytest

from hooks.context import profile_chain
from scripts import install
from scripts.profiles import bundles, render, sources
from scripts.targets._common import shared_directive
from tests.test_user_bundle_install import _skill, _write, clean_manifesto_env, world  # noqa: F401

PERSONAS = [("codex", "AGENTS.md"), ("copilot", "copilot-instructions.md")]


def _hook(command: str) -> dict:
    return {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}


@pytest.fixture
def layered(world):  # noqa: F811
    root = world["root"]
    layer = root / "profiles" / "ship"
    _write(root / "bundle.yml", "layer: profiles/ship\n")
    _write(root / "CLAUDE.md", "DEV DOCTRINE\n")
    _write(root / ".claude" / "CLAUDE.md", "DEV DOT DOCTRINE\n")
    _write(root / ".claude" / "rules" / "dev.md", "DEV RULE\n")
    _write(root / ".claude" / "settings.overrides.json", json.dumps({"env": {"DEV": "1"}}))
    _write(root / ".claude" / ".mcp.json", json.dumps({"mcpServers": {"dev": {"command": "dev-cmd"}}}))
    _write(root / ".codex" / "config.overrides.toml", 'model = "dev-model"\n')
    _skill(root, "dev-skill", "dev")
    _write(layer / "CLAUDE.md", "SHIP PERSONA\n")
    _write(layer / ".claude" / "CLAUDE.md", "SHIP DOCTRINE\n")
    _write(layer / ".claude" / "rules" / "ship.md", "SHIP RULE\n")
    _write(layer / ".claude" / "agents" / "ship-agent.md", "agent\n")
    _write(layer / ".claude" / "commands" / "ship-cmd.md", "---\ndescription: ship\n---\ncommand\n")
    _write(
        layer / ".claude" / "settings.overrides.json",
        json.dumps({"env": {"SHIP": "1"}, "hooks": _hook("echo ship-hook")}),
    )
    _write(layer / ".claude" / ".mcp.json", json.dumps({"mcpServers": {"ship": {"command": "ship-cmd"}}}))
    _write(layer / ".codex" / "config.overrides.toml", 'model = "ship-model"\n')
    _skill(layer, "ship-skill", "ship")
    bundles.link(root)
    return {**world, "layer": layer}


def _install(profile: str = "tiny", target: str = "claude") -> None:
    install.install_global(argparse.Namespace(profile=profile, settings_profile="", install_target=target))


def _settings(found: dict) -> dict:
    return json.loads((found["home"] / ".claude" / "settings.json").read_text())


def _stop_commands(settings: dict) -> list[str]:
    return [h["command"] for group in settings["hooks"]["Stop"] for h in group["hooks"]]


@pytest.mark.feature("bundle-layer-manifest")
def test_a_bundle_without_a_named_layer_ships_from_its_root(tmp_path):
    root = tmp_path / "bundle"
    _write(root / "CLAUDE.md", "ROOT DIRECTIVE\n")

    assert profile_chain.layer_root(root) == root
    for manifest in ("name: plain\n", "layer: .\n", "layer:\n"):
        _write(root / "bundle.yml", manifest)
        assert profile_chain.layer_root(root) == root
        assert profile_chain.doctrine(root) == root / "CLAUDE.md"


@pytest.mark.feature("bundle-layer-manifest")
def test_a_named_layer_resolves_to_its_directory_inside_the_bundle(tmp_path):
    root = tmp_path / "bundle"
    _write(root / "bundle.yml", "layer: ./profiles/ship/\n")

    assert profile_chain.layer_root(root) == root / "profiles" / "ship"


@pytest.mark.feature("bundle-layer-manifest")
@pytest.mark.parametrize("layer", ["../outside", "/etc", "profiles/../../outside", "7"])
def test_a_layer_outside_the_bundle_is_refused_and_the_root_ships(tmp_path, capsys, layer):
    root = tmp_path / "bundle"
    _write(root / "bundle.yml", f"layer: {layer}\n")

    assert profile_chain.layer_root(root) == root
    assert "is not inside the bundle; using the bundle root" in capsys.readouterr().err


@pytest.mark.feature("bundle-layer-manifest")
def test_a_named_layer_keeps_the_root_claude_md_out_of_the_shared_doctrine(layered, clean_manifesto_env):  # noqa: F811
    (layered["layer"] / ".claude" / "CLAUDE.md").unlink()

    assert shared_directive(layered["root"]) == ""
    assert sources.doctrine_files(layered["root"], []) == []


@pytest.mark.feature("bundle-layer-manifest")
def test_a_named_layer_ships_rules_skills_agents_and_commands_and_the_root_dot_claude_stays_behind(layered):
    _install()

    claude = layered["home"] / ".claude"
    assert (claude / "rules" / "ship.md").read_text() == "SHIP RULE\n"
    assert not (claude / "rules" / "dev.md").exists()
    assert (claude / "skills" / "ship-skill").resolve() == layered["layer"] / ".claude" / "skills" / "ship-skill"
    assert not (claude / "skills" / "dev-skill").exists()
    assert (claude / "agents" / "ship-agent.md").is_symlink()
    assert (claude / "commands" / "ship-cmd.md").is_symlink()


@pytest.mark.feature("bundle-layer-manifest")
def test_a_named_layer_ships_settings_mcp_and_doctrine_and_no_root_development_file(layered):
    _install()

    env = _settings(layered)["env"]
    assert env["SHIP"] == "1"
    assert "DEV" not in env
    servers = json.loads((layered["home"] / ".claude.json").read_text())["mcpServers"]
    assert servers["ship"]["command"] == "ship-cmd"
    assert "dev" not in servers
    managed = install._collect_all_managed_mcp_servers()
    assert "ship" in managed
    assert "dev" not in managed
    text = (layered["home"] / ".claude" / "CLAUDE.md").read_text()
    assert text.count("SHIP DOCTRINE") == 1
    assert "DEV DOCTRINE" not in text
    assert "DEV DOT DOCTRINE" not in text


@pytest.mark.feature("bundle-layer-manifest")
@pytest.mark.parametrize(("target", "persona"), PERSONAS)
def test_compiled_personas_take_the_layer_doctrine_and_rules_and_never_the_root_ones(layered, target, persona):
    _install(target=target)

    text = (layered["home"] / f".{target}" / persona).read_text()
    assert text.count("SHIP DOCTRINE") == 1
    assert text.count("SHIP RULE") == 1
    assert "DEV DOCTRINE" not in text
    assert "DEV DOT DOCTRINE" not in text
    assert "DEV RULE" not in text


@pytest.mark.feature("bundle-layer-manifest")
def test_codex_applies_the_layer_config_and_not_the_root_one(layered):
    _install(target="codex")

    assert tomllib.loads((layered["home"] / ".codex" / "config.toml").read_text())["model"] == "ship-model"


@pytest.mark.feature("bundle-layer-manifest")
def test_a_chain_profile_that_is_the_layer_adds_only_its_persona_and_each_hook_once(layered):
    _install("tiny,ship")
    _install("tiny,ship")

    settings = _settings(layered)
    assert _stop_commands(settings).count("echo ship-hook") == 1
    assert settings["env"]["SHIP"] == "1"
    assert "DEV" not in settings["env"]
    text = (layered["home"] / ".claude" / "CLAUDE.md").read_text()
    assert text.count("SHIP DOCTRINE") == 1
    assert text.count("SHIP PERSONA") == 1
    assert text.index("SHIP DOCTRINE") < text.index("SHIP PERSONA")


@pytest.mark.feature("bundle-layer-manifest")
@pytest.mark.parametrize(("target", "persona"), PERSONAS)
def test_a_chain_profile_that_is_the_layer_compiles_its_doctrine_and_persona_once(layered, target, persona):
    _install("tiny,ship", target)

    text = (layered["home"] / f".{target}" / persona).read_text()
    assert [text.count(marker) for marker in ("SHIP DOCTRINE", "SHIP PERSONA", "SHIP RULE")] == [1, 1, 1]


@pytest.mark.feature("bundle-layer-manifest")
def test_doctrine_rows_name_the_layer_doctrine_once_and_the_layer_persona_once(layered, clean_manifesto_env):  # noqa: F811
    layer = layered["layer"]

    assert sources.doctrine_files(layered["root"], [("ship", layer)]) == [
        layer / ".claude" / "CLAUDE.md",
        layer / "CLAUDE.md",
    ]


@pytest.mark.feature("bundle-layer-manifest")
def test_profile_renders_read_features_settings_and_mcp_from_the_layer_once(layered):
    dirs = [("tiny", install.PROFILES_DIR / "tiny"), ("ship", layered["layer"])]

    rules = render._features("rules", lambda item: True, layered["root"], dirs)
    settings = render._settings("claude", layered["root"], dirs)
    servers = render._mcp_servers("claude", layered["root"], dirs)

    assert rules["ship.md"] == layered["layer"] / ".claude" / "rules" / "ship.md"
    assert "dev.md" not in rules
    assert _stop_commands(settings).count("echo ship-hook") == 1
    assert "DEV" not in settings["env"]
    assert "ship" in servers
    assert "dev" not in servers


@pytest.mark.feature("bundle-layer-manifest")
def test_manifestos_and_dependencies_come_from_the_layer(layered, clean_manifesto_env):  # noqa: F811
    from hooks import config
    from scripts import deps_preflight

    root, layer = layered["root"], layered["layer"]
    _write(layer / "manifestos" / "ship.md", "SHIP MANIFESTO\n")
    _write(root / "manifestos" / "dev.md", "DEV MANIFESTO\n")
    plugin = {"kind": "claude-plugin", "check": ["true"], "install": ["true"]}
    _write(layer / "deps.json", json.dumps({"deps": [{"id": "ship@m", **plugin}]}))
    _write(root / "deps.json", json.dumps({"deps": [{"id": "dev@m", **plugin}]}))

    assert config._resolve_manifesto_paths(root) == [str(layer / "manifestos" / "ship.md")]
    assert deps_preflight.fleet_plugins(root) == ["ship@m"]


@pytest.fixture
def runtime(layered, monkeypatch):
    monkeypatch.setattr(profile_chain, "state_path", lambda: install.STATE_JSON)
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    monkeypatch.delenv("AGENTIHOOKS_PROFILE", raising=False)
    state = {"bundle": {"path": str(layered["root"])}, "targets": {"global": {"claude": {"profile": "ship"}}}}
    install.STATE_JSON.write_text(json.dumps(state))
    return layered


@pytest.mark.feature("bundle-layer-manifest")
def test_runtime_conditions_enforcements_workflows_and_lifecycle_read_the_layer_once(runtime, monkeypatch):
    from hooks.context import conditions, enforcement
    from hooks.lifecycle import config
    from scripts.workflows import loader

    root, layer = runtime["root"], runtime["layer"]
    entry = {"message": "m", "cadence": 5}
    _write(layer / "enforcements.json", json.dumps({"enforcements": [{"id": "ship-e", **entry}]}))
    _write(root / "enforcements.json", json.dumps({"enforcements": [{"id": "dev-e", **entry}]}))
    _write(layer / "lifecycle.json", json.dumps({"roots": [{"id": "ship-r", "kind": "scratch", "path": "/s"}]}))
    _write(root / "lifecycle.json", json.dumps({"roots": [{"id": "dev-r", "kind": "scratch", "path": "/d"}]}))
    monkeypatch.setattr(enforcement, "_load_store", lambda: [])
    monkeypatch.setattr(enforcement, "_load_local_enforcements", lambda cwd: [])
    state = profile_chain.read_state()

    layers, _probed = conditions.layer_dirs(state)
    assert [source for source, _ in layers] == ["package", "bundle", "runtime"]
    assert dict(layers)["bundle"] == layer / ".claude" / "conditions"
    assert conditions.target_dir("global") == ("bundle", layer / ".claude" / "conditions")
    assert [(e["id"], e["source"]) for e in enforcement.load_all_enforcements()] == [("ship-e", "bundle")]
    assert loader.layer_dirs(state, None) == [("package", loader.PACKAGE_WORKFLOWS), ("bundle", layer / "workflows")]
    assert {r.id for r in config.load_roots()} >= {"ship-r"}
    assert "dev-r" not in {r.id for r in config.load_roots()}


@pytest.mark.feature("bundle-layer-manifest")
def test_links_into_a_profile_layer_are_managed_and_reaped_when_their_source_goes(layered):
    _install()
    link = layered["home"] / ".claude" / "skills" / "ship-skill"
    source = layered["layer"] / ".claude" / "skills" / "ship-skill"
    assert link.is_symlink()
    assert install._under_managed_source(str(source))

    shutil.rmtree(source)
    _install()

    assert not link.is_symlink()


@pytest.mark.feature("bundle-layer-manifest")
def test_bundle_help_names_the_layer_action_and_it_prints_the_resolved_layer(layered, capsys):
    parser = argparse.ArgumentParser(prog="agentihooks")
    bundles.add_parser(parser.add_subparsers(dest="command"))
    with pytest.raises(SystemExit):
        parser.parse_args(["bundle", "--help"])
    assert re.search(r"\blayer\b", capsys.readouterr().out)

    bundles.cmd_bundle("layer")
    assert capsys.readouterr().out == f"{layered['layer']}\n"

    bundles.cmd_bundle("list")
    assert f"Layer: {layered['layer']}\n" in capsys.readouterr().out
