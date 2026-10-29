import json
from pathlib import Path

from tests.test_profile_render import world as render_world

world = render_world
JUDGE = Path(__file__).resolve().parents[1] / "profiles" / "package" / "roles" / "judge"


def test_judge_role_resolves_on_the_install_path(world):
    install = world["install"]
    install._save_state({})

    assert (install._resolve_profile_dir("judge"), "judge" in install._available_profiles()) == (JUDGE, True)


def test_judge_role_renders_its_doctrine_with_no_mcp_or_conditions(world, monkeypatch):
    from hooks.context import conditions
    from scripts.profiles import render

    world["install"]._save_state({})
    out = render.render_claude("judge")

    assert (JUDGE / "CLAUDE.md").read_text().strip() in (out / "CLAUDE.md").read_text()
    assert json.loads((out / render.STAMP).read_text())["chain"] == ["judge"]
    assert "mcpServers" not in json.loads((out / "settings.json").read_text())
    assert sorted(path.name for path in JUDGE.iterdir()) == ["CLAUDE.md", "profile.yml"]
    monkeypatch.setenv("AGENTIHOOKS_PROFILE", "judge")
    layers = conditions.layer_dirs({})[0]
    assert ("profile:judge", JUDGE / ".claude" / "conditions") in layers
    entries, invalid = conditions.scan_layers(layers)
    assert ([e for e in entries if e["source"] == "profile:judge"], invalid) == ([], [])
