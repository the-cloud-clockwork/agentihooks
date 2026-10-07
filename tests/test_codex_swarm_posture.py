import tomllib

import pytest

from tests.test_profile_render import world as render_world

world = render_world

ROLES = ("engineer", "master", "qa", "planner", "cicd")


def _posture(out) -> tuple[str, str]:
    doc = tomllib.loads((out / "config.toml").read_text())
    return doc["approval_policy"], doc["sandbox_mode"]


@pytest.mark.parametrize("role", ROLES)
def test_a_packaged_swarm_role_renders_full_autonomy_without_a_bundle(world, role):
    from scripts.profiles import render

    world["install"]._save_state({})

    first = render.render_codex(role)
    second = render.render_codex(role, force=True)

    assert first == second == render.rendered_root() / role / "codex"
    assert _posture(first) == ("never", "danger-full-access")
    assert render.stamp(role)["chain"] == [role]


@pytest.mark.parametrize("role", ROLES)
def test_an_explicit_packaged_swarm_role_renders_full_autonomy_beside_a_bundle(world, role):
    from scripts.profiles import render

    out = render.render_codex(f"package:{role}", force=True)

    assert _posture(out) == ("never", "danger-full-access")
    assert _posture(render.render_codex(f"package:{role}", force=True)) == ("never", "danger-full-access")


@pytest.mark.parametrize("target", ("claude", "codex"))
def test_a_render_refuses_a_linked_bundle_whose_folder_is_gone(world, tmp_path, target):
    from scripts.profiles import render

    gone = tmp_path / "gone" / "bundle"
    world["install"]._save_state({"bundle": {"path": str(gone)}})

    with pytest.raises(ValueError) as caught:
        render.render(target, "engineer")

    assert str(caught.value) == (
        f"linked bundle {gone} is missing; relink it with agentihooks bundle link <path> before rendering"
    )
    assert not (render.rendered_root() / "engineer").exists()


def test_a_stamp_refuses_a_linked_bundle_whose_folder_is_gone(world, tmp_path):
    from scripts.profiles import render

    world["install"]._save_state({"bundle": {"path": str(tmp_path / "gone")}})

    with pytest.raises(ValueError, match="^linked bundle .*gone is missing"):
        render.stamp("engineer")


def test_a_scratch_render_refuses_a_linked_bundle_whose_folder_is_gone(world, tmp_path, capsys):
    from scripts.profiles import render

    gone = tmp_path / "gone"
    world["install"]._save_state({"bundle": {"path": str(gone)}})
    out = tmp_path / "scratch"

    assert render.main(["render", "engineer", "--out", str(out)]) == 1

    assert capsys.readouterr().err == (
        f"ERROR: linked bundle {gone} is missing; relink it with agentihooks bundle link <path> before rendering\n"
    )
    assert not out.exists()
