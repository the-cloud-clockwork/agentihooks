import hashlib
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

SHARED = {"projects", "sessions", "todos", "plans", "plugins", ".credentials.json"}


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _git(bundle: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t"}
    env["GIT_COMMITTER_EMAIL"] = "t@t"
    return subprocess.run(["git", *args], cwd=bundle, env=env, check=True, capture_output=True, text=True).stdout


def _commit(bundle: Path, message: str) -> None:
    _git(bundle, "add", "-A")
    _git(bundle, "commit", "-q", "--allow-empty", "-m", message)


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture
def world(tmp_path, monkeypatch):
    from scripts.targets._common import _install_module

    install = _install_module()
    python = tmp_path / "venv" / "bin" / "python"
    monkeypatch.setattr(install, "_detect_venv", lambda: python)
    home = Path.home()
    bundle = tmp_path / "bundle"
    _write(bundle / ".claude" / "CLAUDE.md", "BUNDLE DIRECTIVE MARKER\n")
    _write(bundle / ".claude" / "skills" / "bundle-skill" / "SKILL.md", "---\nname: bundle-skill\n---\n")
    _write(bundle / ".claude" / "rules" / "bundle-rule.md", "BUNDLE RULE MARKER\n")
    _write(bundle / ".claude" / "settings.overrides.json", json.dumps({"env": {"BUNDLE_FLAG": "1"}}))
    _write(bundle / ".claude" / ".mcp.json", json.dumps({"mcpServers": {"bundle-srv": {"command": "bundle-server"}}}))
    _write(bundle / ".codex" / "config.overrides.toml", 'approval_policy = "never"\n')
    profiles = bundle / "profiles"
    _write(profiles / "rb-base" / "profile.yml", "name: rb-base\n")
    _write(profiles / "rb-base" / "CLAUDE.md", "BASE PERSONA MARKER\n")
    _write(profiles / "rb-base" / ".codex" / "config.overrides.toml", 'sandbox_mode = "workspace-write"\n')
    _write(profiles / "rb-kit" / "profile.yml", "name: rb-kit\n")
    _write(profiles / "rb-kit" / ".claude" / "skills" / "role-skill" / "SKILL.md", "---\nname: role-skill\n---\n")
    _write(profiles / "rb-kit" / ".claude" / "rules" / "role-rule.md", "ROLE RULE MARKER\n")
    _write(profiles / "rb-kit" / ".claude" / "rules" / "README.md", "KIT README\n")
    _write(profiles / "rb-kit" / ".claude" / "rules" / "notes.txt", "KIT NOTES\n")
    _write(profiles / "rb-role" / "profile.yml", "name: rb-role\nextends: [rb-base, rb-kit]\n")
    _write(profiles / "rb-role" / "CLAUDE.md", "ROLE PERSONA MARKER\n")
    _write(profiles / "rb-role" / "hooks" / "role.sh", "true\n")
    hooks = {"Stop": [{"hooks": [{"type": "command", "command": "bash hooks/role.sh"}]}]}
    _write(profiles / "rb-role" / ".claude" / "settings.overrides.json", json.dumps({"hooks": hooks}))
    servers = {
        "role-srv": {"command": "role-server"},
        "leaky-arg": {"command": "srv", "args": ["--token", "ghp_" + "a" * 36]},
        "leaky-env": {"command": "srv", "env": {"GH_TOKEN": "ghp_" + "b" * 36, "SAFE": "${REF}"}},
    }
    _write(profiles / "rb-role" / ".claude" / ".mcp.json", json.dumps({"mcpServers": servers}))
    _write(profiles / "rb-other" / "profile.yml", "name: rb-other\n")
    _write(profiles / "rb-other" / ".claude" / "skills" / "other-skill" / "SKILL.md", "---\nname: other-skill\n---\n")
    _git(bundle, "init", "-q")
    _commit(bundle, "one")
    install._save_state({"bundle": {"path": str(bundle)}})

    claude = home / ".claude"
    _write(claude / "settings.json", json.dumps({"model": "opus", "theme": "dark"}))
    (claude / "projects").mkdir()
    gmail = {"type": "http", "url": "https://gmail.example"}
    claude_json = {"hasCompletedOnboarding": True, "mcpServers": {"google-gmail": gmail}, "projects": {"/w": {}}}
    _write(home / ".claude.json", json.dumps(claude_json))
    codex_mcp = "[mcp_servers.google-gmail]\nurl = 'https://gmail.example'\n\n[mcp_servers.role-srv]\ncommand = 'r'\n"
    codex_mcp += "\n[mcp_servers.bundle-srv]\ncommand = 'b'\n"
    _write(home / ".codex" / "config.toml", codex_mcp)
    skills = home / ".agents" / "skills"
    for name in ("role-skill", "other-skill", "bundle-skill"):
        (skills / name).mkdir()
    _write(skills / "stray.md", "not a skill\n")
    return {"home": home, "bundle": bundle, "install": install, "python": python, "role": profiles / "rb-role"}


def _tree_hashes(root: Path, skip: Path) -> dict[str, str]:
    out = {}
    for path in sorted(root.rglob("*")):
        if path == skip or skip in path.parents or not path.is_file() or path.is_symlink():
            continue
        out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def test_claude_render_tree(world, capsys):
    from scripts.profiles import render

    home, install = world["home"], world["install"]
    out = render.render_claude("rb-role")

    assert out == home / ".agentihooks" / "profiles" / "rb-role" / "claude"
    package_skills = {p.name for p in (install.PACKAGE_FEATURES_DIR / "skills").iterdir() if p.is_dir()}
    skills = {p.name for p in (out / "skills").iterdir()}
    assert skills == package_skills | {"role-skill", "bundle-skill"}
    assert all((out / "skills" / name).is_symlink() for name in skills)
    rules = {p.name for p in (out / "rules").iterdir()}
    assert {"role-rule.md", "bundle-rule.md"} <= rules
    assert not {"README.md", "notes.txt"} & rules
    assert (out / "rules" / "role-rule.md").read_text() == "ROLE RULE MARKER\n"
    persona = (out / "CLAUDE.md").read_text()
    for marker in ("BUNDLE DIRECTIVE MARKER", "BASE PERSONA MARKER", "ROLE PERSONA MARKER"):
        assert marker in persona
    assert "ROLE RULE MARKER" not in persona
    assert persona.startswith(render.HEADER)
    assert persona.endswith(f"\n\n{render.FOOTER}\n")


def test_claude_render_rules_stay_inside_profile_home(world):
    from scripts.profiles import render

    out = render.render_claude("rb-role")

    rules = list((out / "rules").iterdir())
    assert rules
    assert all(path.resolve().is_relative_to(out) for path in rules)
    assert (out / "rules" / "bundle-rule.md").read_text() == "BUNDLE RULE MARKER\n"
    assert (out / "rules" / "role-rule.md").read_text() == "ROLE RULE MARKER\n"


def test_claude_render_upgrades_cached_external_rules(world):
    from scripts.profiles import render

    out = render.render_claude("rb-role")
    rule = out / "rules" / "role-rule.md"
    rule.unlink()
    rule.symlink_to(world["bundle"] / "profiles" / "rb-kit" / ".claude" / "rules" / "role-rule.md")
    (out / render.STAMP).write_text(json.dumps(render.stamp("rb-role")))

    assert render.render_claude("rb-role") == out
    assert rule.resolve().is_relative_to(out)
    assert rule.read_text() == "ROLE RULE MARKER\n"
    assert render.render_claude("rb-role") is None


def test_claude_render_refreshes_copied_rules(world):
    from scripts.profiles import render

    out = render.render_claude("rb-role")
    rules = out / "rules"
    _write(rules / "stale.md", "STALE RULE\n")
    (rules / "broken.md").symlink_to(out / "missing.md")
    (rules / "local-folder").mkdir()
    source = world["bundle"] / ".claude" / "rules" / "bundle-rule.md"
    _write(source, "---\npaths: ['**/*.py']\n---\nUPDATED BUNDLE RULE\n")

    assert render.render_claude("rb-role", force=True) == out
    assert not (rules / "stale.md").exists()
    assert not (rules / "broken.md").is_symlink()
    assert (rules / "local-folder").is_dir()
    assert (rules / "bundle-rule.md").read_text() == "---\npaths: ['**/*.py']\n---\nUPDATED BUNDLE RULE\n"


def test_refresh_rules_updates_rendered_profile_copies(world, monkeypatch):
    from scripts.profiles import render
    from scripts.targets.claude_target import refresh_rules

    out = render.render_claude("rb-role")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(out))
    source = world["bundle"] / ".claude" / "rules" / "bundle-rule.md"
    _write(source, "UPDATED RENDERED RULE\n")
    _write(out / "CLAUDE.local.md", "ROLE LOCAL OVERRIDE\n")

    payload = refresh_rules(out / "rules", out / "CLAUDE.md", out / "CLAUDE.local.md", False)

    assert (out / "rules" / "bundle-rule.md").read_text() == "UPDATED RENDERED RULE\n"
    assert "UPDATED RENDERED RULE" in payload
    assert "ROLE PERSONA MARKER" in payload
    assert "ROLE LOCAL OVERRIDE" in payload


def test_claude_render_settings(world):
    from scripts.profiles import render

    out = render.render_claude("rb-role")

    text = (out / "settings.json").read_text()
    assert "__PYTHON__" not in text
    assert str(world["python"]) in text
    settings = json.loads(text)
    assert settings["model"] == "opus"
    assert "theme" not in settings
    assert settings["env"]["BUNDLE_FLAG"] == "1"
    resolved = str((world["role"] / "hooks" / "role.sh").resolve())
    assert {"type": "command", "command": f"bash {resolved}"} in settings["hooks"]["Stop"][-1]["hooks"]


def test_claude_render_enables_operator_fleet_and_chain_plugins(world):
    from scripts.profiles import render

    home, bundle = world["home"], world["bundle"]
    operator = {"mine@m": True, "off@m": False, "fleet@m": False}
    _write(home / ".claude" / "settings.json", json.dumps({"model": "opus", "enabledPlugins": operator}))
    plugin = {"kind": "claude-plugin", "check": ["true"], "install": ["true"]}
    deps = [{**plugin, "id": "fleet@m"}, {**plugin, "id": "muted@m"}, {**plugin, "id": "gone@m", "state": "absent"}]
    _write(bundle / "deps.json", json.dumps({"deps": deps}))
    kit = {"enabledPlugins": {"kit@m": True, "off@m": True, "muted@m": False}}
    _write(bundle / "profiles" / "rb-kit" / ".claude" / "settings.overrides.json", json.dumps(kit))

    out = render.render_claude("rb-role")

    assert json.loads((out / "settings.json").read_text())["enabledPlugins"] == {
        "mine@m": True,
        "off@m": True,
        "fleet@m": True,
        "muted@m": False,
        "kit@m": True,
    }


def test_claude_render_excludes_default_home_instructions(world):
    from scripts.profiles import render

    out = render.render_claude("rb-role")

    excludes = json.loads((out / "settings.json").read_text())["claudeMdExcludes"]
    default_home = world["home"] / ".claude"
    assert excludes == [str(default_home / "CLAUDE.md"), str(default_home / "rules" / "**")]


def test_claude_render_mcp_and_shared_data(world, capsys):
    from scripts.profiles import render

    home, install = world["home"], world["install"]
    out = render.render_claude("rb-role")

    claude_json = json.loads((out / ".claude.json").read_text())
    agentihooks = install._build_mcp_config("all")["mcpServers"]["agentihooks"]
    assert claude_json["mcpServers"] == {
        "agentihooks": agentihooks,
        "bundle-srv": {"command": "bundle-server"},
        "role-srv": {"command": "role-server"},
        "leaky-env": {"command": "srv", "env": {"SAFE": "${REF}"}},
    }
    printed = capsys.readouterr().out
    assert "MCP 'leaky-arg' carries credential-shaped literals in args[1]" in printed
    assert "MCP 'leaky-env' env var 'GH_TOKEN' looks like a credential" in printed
    assert printed.count(f"from {out / '.claude.json'}") == 1
    assert printed.count(f"written to {out / '.claude.json'}") == 1
    assert claude_json["hasCompletedOnboarding"] is True
    assert claude_json["projects"] == {"/w": {}}
    links = {p.name for p in out.iterdir() if p.is_symlink()}
    assert links == SHARED
    for name in SHARED:
        assert os.readlink(out / name) == str(home / ".claude" / name)


def test_claude_render_from_inside_a_rendered_home(world, monkeypatch):
    from scripts.profiles import render

    home = world["home"]
    elsewhere = home / "elsewhere"
    _write(elsewhere / "settings.json", json.dumps({"model": "haiku"}))
    _write(elsewhere / ".claude.json", json.dumps({"hasCompletedOnboarding": False}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(elsewhere))

    out = render.render_claude("rb-role")

    assert json.loads((out / "settings.json").read_text())["model"] == "opus"
    assert json.loads((out / ".claude.json").read_text())["hasCompletedOnboarding"] is True
    assert os.readlink(out / "projects") == str(home / ".claude" / "projects")


def test_claude_render_keeps_runtime_state_of_previous_render(world):
    from scripts.profiles import render

    out = render.render_claude("rb-role")
    claude_json = json.loads((out / ".claude.json").read_text())
    claude_json["numStartups"] = 7
    claude_json["mcpServers"]["stale"] = {"command": "x"}
    (out / ".claude.json").write_text(json.dumps(claude_json))
    (out / "skills" / "gone").symlink_to(out)

    assert render.render_claude("rb-role") is None
    assert render.render_claude("rb-role", force=True) == out

    claude_json = json.loads((out / ".claude.json").read_text())
    assert claude_json["numStartups"] == 7
    assert "stale" not in claude_json["mcpServers"]
    assert not (out / "skills" / "gone").is_symlink()


def test_codex_render_writes_one_layered_profile(world):
    from scripts.profiles import render

    home = world["home"]
    before = _tree_hashes(home, home / ".agentihooks" / "profiles")

    path = render.render_codex("rb-role")

    assert path == home / ".codex" / "rb-role.config.toml"
    after = _tree_hashes(home, home / ".agentihooks" / "profiles")
    assert set(after) - set(before) == {".codex/rb-role.config.toml"}
    assert {k: v for k, v in after.items() if k in before} == before
    doc = tomllib.loads(path.read_text())
    instructions = doc["developer_instructions"]
    for marker in ("BUNDLE DIRECTIVE MARKER", "BASE PERSONA MARKER", "ROLE RULE MARKER", "BUNDLE RULE MARKER"):
        assert marker in instructions
    assert "<!-- rule: role-rule.md (rule) -->" in instructions
    assert instructions.endswith(f"\n\n{render.FOOTER}\n")
    assert "KIT README" not in instructions
    assert doc["sandbox_mode"] == "workspace-write"
    assert doc["approval_policy"] == "never"
    assert "model_context_window" not in doc
    assert doc["mcp_servers"] == {"google-gmail": {"enabled": False}}
    other = str(home / ".agents" / "skills" / "other-skill" / "SKILL.md")
    assert doc["skills"]["config"] == [{"path": other, "enabled": False}]
    assert doc["agentihooks"]["render"] == render.stamp("rb-role")
    assert render.render_codex("rb-role") is None


@pytest.mark.parametrize("config", [None, 'model = "gpt"\n'])
def test_codex_render_without_global_servers_or_skills(world, config):
    from scripts.profiles import render

    home = world["home"]
    (home / ".codex" / "config.toml").unlink()
    if config:
        _write(home / ".codex" / "config.toml", config)
    for skill in (home / ".agents" / "skills").iterdir():
        skill.unlink() if skill.is_file() else skill.rmdir()

    doc = tomllib.loads(render.render_codex("rb-role").read_text())

    assert "mcp_servers" not in doc
    assert "skills" not in doc


def test_codex_render_replaces_an_unstamped_profile_file(world):
    from scripts.profiles import render

    path = _write(world["home"] / ".codex" / "rb-role.config.toml", 'model = "hand-written"\n')

    assert render.render_codex("rb-role") == path
    assert "hand-written" not in path.read_text()


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_render_leaves_global_install_untouched(world, target):
    from scripts.profiles import render

    home = world["home"]
    skip = home / ".agentihooks" / "profiles"
    before = _tree_hashes(home, skip)

    render.render(target, "rb-role")

    after = _tree_hashes(home, skip)
    if target == "codex":
        after.pop(".codex/rb-role.config.toml")
    assert after == before


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_stamp_skips_fresh_render_and_redoes_stale(world, target):
    from scripts.profiles import render

    bundle = world["bundle"]
    assert render.render(target, "rb-role") is not None
    assert render.render(target, "rb-role") is None
    assert render.render(target, "rb-role", force=True) is not None

    _commit(bundle, "two")
    assert render.render(target, "rb-role") is not None
    assert render.render(target, "rb-role") is None

    _write(bundle / "profiles" / "rb-role" / "profile.yml", "name: rb-role\nextends: [rb-base, rb-kit, rb-other]\n")
    assert render.render(target, "rb-role") is not None


def test_stamp_names_bundle_commit_and_chain(world):
    from scripts.profiles import render

    head = _git(world["bundle"], "rev-parse", "HEAD").strip()
    assert render.stamp("rb-role") == {"bundle_commit": head, "chain": ["rb-base", "rb-kit", "rb-role"]}
    assert render._stamp(None, []) == {"bundle_commit": "", "chain": []}
    assert render._roots(None, [("rb-role", world["role"])]) == [world["role"]]


def test_render_refuses_other_targets(world):
    from scripts.profiles import render

    with pytest.raises(ValueError, match="^copilot per-run profiles are not supported$"):
        render.render("copilot", "rb-role")


def test_cli_renders_and_refuses(world, capsys):
    from scripts.profiles import render

    out = Path.home() / ".agentihooks" / "profiles" / "rb-role" / "claude"
    assert render.main(["render", "rb-role"]) == 0
    assert capsys.readouterr().out.endswith(f"\nRendered rb-role (claude) → {out}\n")
    assert render.main(["render", "rb-role", "--target", "claude"]) == 0
    assert capsys.readouterr().out == "rb-role (claude) is up to date\n"
    assert render.main(["render", "rb-role", "--force"]) == 0
    assert capsys.readouterr().out.endswith(f"\nRendered rb-role (claude) → {out}\n")
    assert render.main(["render", "rb-role", "--target", "copilot"]) == 2
    assert capsys.readouterr().err == "copilot per-run profiles are not supported\n"
    assert render.main(["render", "rb-missing", "--target", "codex"]) == 1
    assert capsys.readouterr().err == "ERROR: Profile 'rb-missing' not found\n"


def test_cli_usage(world, capsys):
    from scripts.profiles import render

    with pytest.raises(SystemExit):
        render.main(["render", "rb-role", "--target", "bogus"])
    assert re.search(
        r"invalid choice: 'bogus' \(choose from '?claude'?, '?codex'?, '?copilot'?\)", capsys.readouterr().err
    )
    with pytest.raises(SystemExit):
        render.main([])
    assert capsys.readouterr().err.startswith("usage: agentihooks profile [-h] {render}")
    with pytest.raises(SystemExit):
        render.main(["--help"])
    assert re.search(
        r"(?<!\S)render Render a profile into its own home for one harness(?!\S)", _flat(capsys.readouterr().out)
    )


def test_agentihooks_profile_dispatches_to_render(monkeypatch, capsys):
    from scripts import install

    monkeypatch.setattr("sys.argv", ["agentihooks", "profile", "render", "rb-role", "--target", "copilot"])
    with pytest.raises(SystemExit) as exc:
        install.main()
    assert exc.value.code == 2
    assert capsys.readouterr().err == "copilot per-run profiles are not supported\n"


def test_agentihooks_help_lists_profile(monkeypatch, capsys):
    from scripts import install

    monkeypatch.setattr("sys.argv", ["agentihooks", "--help"])
    with pytest.raises(SystemExit):
        install.main()
    line = r"(?<!\S)profile Render a profile into its own home: render NAME --target claude\|codex \[--force\](?!\S)"
    assert re.search(line, _flat(capsys.readouterr().out))
