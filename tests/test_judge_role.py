import json
from pathlib import Path

from tests.test_profile_render import world as render_world

world = render_world
JUDGE = Path(__file__).resolve().parents[1] / "profiles" / "package" / "roles" / "judge"


def test_judge_role_resolves_from_the_package():
    from hooks.context import profile_chain

    assert profile_chain.profile_dirs(None, "judge", {}) == [("judge", JUDGE)]


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
    entries, invalid = conditions.scan_layers(conditions.layer_dirs({})[0])
    assert ([e for e in entries if e["source"] == "profile:judge"], invalid) == ([], [])
