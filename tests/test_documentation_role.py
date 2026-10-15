import json

from scripts.profiles import render
from tests.test_profile_render import world as world


def test_documentation_role_has_its_own_skill_and_only_task_browser(world):
    world["install"]._save_state({})
    out = render.render_claude("documentation")
    assert "Role: Documentation" in (out / "CLAUDE.md").read_text()
    assert (out / "skills/create-simple-docs/SKILL.md").is_file()
    config = json.loads((out / ".claude.json").read_text())
    assert set(config["mcpServers"]) == {"playwright"}
    engineer = render.render_claude("engineer")
    assert not (engineer / "skills/create-simple-docs").exists()


def test_bundle_can_extend_documentation_destination_and_skill(world):
    profile = world["bundle"] / "profiles/documentation"
    profile.mkdir(parents=True)
    (profile / "profile.yml").write_text("name: documentation\nextends: [package:documentation]\n")
    (profile / "CLAUDE.md").write_text("Publish documentation in handbook with the team format.\n")
    skill = profile / ".claude/skills/create-simple-docs"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: create-simple-docs\n---\nWrite handbook pages.\n")
    out = render.render_claude("documentation")
    persona = (out / "CLAUDE.md").read_text()
    assert persona.index("Role: Documentation") < persona.index("Publish documentation in handbook")
    assert "Write handbook pages." in (out / "skills/create-simple-docs/SKILL.md").read_text()
