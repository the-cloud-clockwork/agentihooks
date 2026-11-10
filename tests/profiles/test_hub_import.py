import json
from pathlib import Path

import pytest
import yaml

from scripts.profiles.hub_import import import_hub


@pytest.fixture
def hub(tmp_path: Path) -> Path:
    root = tmp_path / "hub"
    package = root / "agents" / "diagram" / "package"
    (package / ".claude" / "skills" / "render").mkdir(parents=True)
    (package / ".claude" / "rules").mkdir()
    (package / "command.yml").write_text(
        yaml.safe_dump(
            {
                "name": "diagram",
                "description": "Draw diagrams",
                "capabilities": ["diagrams"],
                "claude": {"model": "opus", "effort": "high", "max_turns": 100},
            }
        )
    )
    (package / "CLAUDE.md").write_text("Draw diagrams.\n")
    (package / ".claude/skills/render/SKILL.md").write_text("Render diagrams.\n")
    (package / ".claude/rules/render.md").write_text("Keep source.\n")
    (package / ".claude/settings.json").write_text(
        json.dumps(
            {
                "model": "opus",
                "env": {"OUTPUT": "/home/operator/output", "KEY": "${DIAGRAM_KEY}"},
                "permissions": {"allow": ["Read", "Read(/home/operator/**)"]},
                "hooks": {"Stop": [{"command": "python /home/operator/stop.py"}]},
            }
        )
    )
    return root


@pytest.mark.feature("hub-import-core")
def test_import_converts_metadata_and_copies_content(hub: Path, tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    report = import_hub(hub, out, False)
    profile = out / "profiles/diagram"
    assert report["profiles"] == ["diagram"]
    assert yaml.safe_load((profile / "profile.yml").read_text()) == {
        "name": "diagram",
        "description": "Draw diagrams",
        "capabilities": ["diagrams"],
        "claude": {"model": "opus", "effort": "high"},
    }
    for relative in ("CLAUDE.md", ".claude/skills/render/SKILL.md", ".claude/rules/render.md"):
        assert (profile / relative).read_bytes() == (hub / "agents/diagram/package" / relative).read_bytes()
    assert not (profile / "command.yml").exists()


@pytest.mark.feature("hub-import-core")
def test_settings_remove_absolute_paths_but_keep_environment_references(hub: Path, tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    import_hub(hub, out, False)
    settings = json.loads((out / "profiles/diagram/.claude/settings.overrides.json").read_text())
    assert settings == {
        "model": "opus",
        "env": {"KEY": "${DIAGRAM_KEY}"},
        "permissions": {"allow": ["Read"]},
        "hooks": {"Stop": [{}]},
    }


@pytest.mark.feature("hub-import-core")
def test_dry_run_reports_the_same_files_without_writing(hub: Path, tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    preview = import_hub(hub, out, True)
    assert not out.exists()
    actual = import_hub(hub, out, False)
    assert preview | {"dry_run": False} == actual


@pytest.mark.feature("hub-import-core")
@pytest.mark.parametrize("name", [".env", ".env.local", "credentials.json", "id_rsa", "key.pem"])
def test_secret_files_refuse_the_entire_import(hub: Path, tmp_path: Path, name: str) -> None:
    (hub / "agents/diagram/package/.claude/rules" / name).touch()
    out = tmp_path / "bundle"
    with pytest.raises(ValueError, match="secret"):
        import_hub(hub, out, False)
    assert not out.exists()


@pytest.mark.feature("hub-import-core")
def test_taken_profile_names_are_never_overwritten(hub: Path, tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    existing = out / "profiles/diagram"
    existing.mkdir(parents=True)
    (existing / "CLAUDE.md").write_text("Existing")
    with pytest.raises(ValueError, match="taken"):
        import_hub(hub, out, False)
    assert (existing / "CLAUDE.md").read_text() == "Existing"
    assert list(existing.iterdir()) == [existing / "CLAUDE.md"]


@pytest.mark.feature("hub-import-core")
@pytest.mark.parametrize("name", ["../escape", "/escape", "..", ""])
def test_unsafe_profile_names_are_refused(hub: Path, tmp_path: Path, name: str) -> None:
    command = hub / "agents/diagram/package/command.yml"
    command.write_text(yaml.safe_dump({"name": name}))
    with pytest.raises(ValueError, match="name"):
        import_hub(hub, tmp_path / "bundle", False)


@pytest.mark.feature("hub-import-core")
def test_symlinks_are_refused_without_following_them(hub: Path, tmp_path: Path) -> None:
    (hub / "agents/diagram/package/.claude/rules/external").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        import_hub(hub, tmp_path / "bundle", False)
