from scripts.profiles import codex_master, render
from tests.test_profile_render import _commit, _write
from tests.test_profile_render import world as world


def test_a_push_codex_master_renders_its_persona_without_the_wait_inbox_loop(world):
    _write(world["bundle"] / "profiles" / "master" / "profile.yml", "name: master\nextends: [package:master]\n")
    _write(world["bundle"] / ".claude" / "rules" / "waiting.md", "# Process Watching\n\n- Re-arm the Monitor.\n")
    _commit(world["bundle"], "master")

    out = render.render("codex", "master", push=True)
    persona = (out / "AGENTS.md").read_text()
    assert "wait --inbox" not in persona.replace(codex_master.waiting("<slug>", push=True), "")
    assert persona.endswith("\n" + codex_master.waiting("<slug>", push=True) + "\n")
    assert "- Read agentihooks msg inbox.\n" in persona
    assert "Monitor" not in persona
    assert render.render_codex("master", push=True) is None
    legacy = render.render_codex("master")
    assert legacy is not None
    assert "agentihooks swarm <slug> wait --inbox in a foreground tool call" in (legacy / "AGENTS.md").read_text()
