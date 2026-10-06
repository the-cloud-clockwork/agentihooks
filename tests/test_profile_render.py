import hashlib
import json
import os
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


@pytest.fixture
def world(tmp_path):
    from scripts.targets._common import _install_module

    install = _install_module()

    home = Path.home()
    bundle = tmp_path / "bundle"
    profiles = bundle / "profiles"
    _write(profiles / "rb-base" / "profile.yml", "name: rb-base\n")
    _write(profiles / "rb-base" / "CLAUDE.md", "BASE PERSONA MARKER\n")
    _write(profiles / "rb-base" / ".codex" / "config.overrides.toml", 'sandbox_mode = "workspace-write"\n')
    _write(profiles / "rb-kit" / "profile.yml", "name: rb-kit\n")
    _write(profiles / "rb-kit" / ".claude" / "skills" / "role-skill" / "SKILL.md", "---\nname: role-skill\n---\n")
    _write(profiles / "rb-kit" / ".claude" / "rules" / "role-rule.md", "ROLE RULE MARKER\n")
    _write(profiles / "rb-role" / "profile.yml", "name: rb-role\nextends: [rb-base, rb-kit]\n")
    _write(profiles / "rb-role" / "CLAUDE.md", "ROLE PERSONA MARKER\n")
    servers = {"mcpServers": {"role-srv": {"command": "role-server"}}}
    _write(profiles / "rb-role" / ".claude" / ".mcp.json", json.dumps(servers))
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
    _write(home / ".codex" / "config.toml", codex_mcp)
    skills = home / ".agents" / "skills"
    for name in ("role-skill", "other-skill"):
        (skills / name).mkdir()
    return {"home": home, "bundle": bundle}


def _tree_hashes(root: Path, skip: Path) -> dict[str, str]:
    out = {}
    for path in sorted(root.rglob("*")):
        if path == skip or skip in path.parents or not path.is_file() or path.is_symlink():
            continue
        out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def test_claude_render_tree(world):
    from scripts.profiles import render

    home = world["home"]
    out = render.render_claude("rb-role")

    assert out == home / ".agentihooks" / "profiles" / "rb-role" / "claude"
    assert (out / "skills" / "role-skill").is_symlink()
    assert not (out / "skills" / "other-skill").exists()
    assert (out / "rules" / "role-rule.md").read_text() == "ROLE RULE MARKER\n"
    persona = (out / "CLAUDE.md").read_text()
    assert "BASE PERSONA MARKER" in persona
    assert "ROLE PERSONA MARKER" in persona
    settings = json.loads((out / "settings.json").read_text())
    assert settings["hooks"]
    assert settings["model"] == "opus"
    assert "theme" not in settings
    claude_json = json.loads((out / ".claude.json").read_text())
    assert set(claude_json["mcpServers"]) == {"agentihooks", "role-srv"}
    assert claude_json["hasCompletedOnboarding"] is True
    assert claude_json["projects"] == {"/w": {}}
    links = {p.name for p in out.iterdir() if p.is_symlink()}
    assert links == SHARED
    for name in SHARED:
        assert os.readlink(out / name) == str(home / ".claude" / name)


def test_claude_render_keeps_runtime_state_of_previous_render(world):
    from scripts.profiles import render

    out = render.render_claude("rb-role")
    claude_json = json.loads((out / ".claude.json").read_text())
    claude_json["numStartups"] = 7
    claude_json["mcpServers"]["stale"] = {"command": "x"}
    (out / ".claude.json").write_text(json.dumps(claude_json))

    render.render_claude("rb-role", force=True)

    claude_json = json.loads((out / ".claude.json").read_text())
    assert claude_json["numStartups"] == 7
    assert set(claude_json["mcpServers"]) == {"agentihooks", "role-srv"}


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
    assert "BASE PERSONA MARKER" in doc["developer_instructions"]
    assert "ROLE RULE MARKER" in doc["developer_instructions"]
    assert doc["sandbox_mode"] == "workspace-write"
    assert doc["mcp_servers"] == {"google-gmail": {"enabled": False}}
    other = str(home / ".agents" / "skills" / "other-skill" / "SKILL.md")
    assert doc["skills"]["config"] == [{"path": other, "enabled": False}]


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


def test_cli_renders_and_refuses(world, capsys):
    from scripts.profiles import render

    assert render.main(["render", "rb-role", "--target", "claude"]) == 0
    assert str(Path.home() / ".agentihooks" / "profiles" / "rb-role" / "claude") in capsys.readouterr().out
    assert render.main(["render", "rb-role", "--target", "claude"]) == 0
    assert "up to date" in capsys.readouterr().out
    assert render.main(["render", "rb-role", "--target", "copilot"]) == 2
    assert "copilot per-run profiles are not supported" in capsys.readouterr().err
    assert render.main(["render", "rb-missing", "--target", "codex"]) == 1
    assert "rb-missing" in capsys.readouterr().err


def test_agentihooks_profile_dispatches_to_render(world, monkeypatch, capsys):
    from scripts.targets._common import _install_module

    monkeypatch.setattr("sys.argv", ["agentihooks", "profile", "render", "rb-role", "--target", "copilot"])
    with pytest.raises(SystemExit) as exc:
        _install_module().main()
    assert exc.value.code == 2
    assert "copilot per-run profiles are not supported" in capsys.readouterr().err
