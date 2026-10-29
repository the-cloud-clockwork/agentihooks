import json
import shutil
from pathlib import Path

import pytest

from tests import test_profile_render as base

_write, _git, _commit = base._write, base._git, base._commit
world, overlays, recorded_assets = base.world, base.overlays, base.recorded_assets


@pytest.fixture
def user(world, tmp_path):
    root = tmp_path / "user-bundle"
    _write(root / ".claude" / "CLAUDE.md", "USER DIRECTIVE v1\n")
    _write(root / ".claude" / "skills" / "personal" / "SKILL.md", "---\nname: personal\n---\n")
    _write(root / "profiles" / "ov-user" / "profile.yml", "name: ov-user\nkind: overlay\nwears: [engineer]\n")
    _write(root / "profiles" / "ov-user" / ".claude" / "rules" / "ov-user.md", "OV-USER RULE v1\n")
    _git(root, "init", "-q")
    _commit(root, "one")
    from scripts.profiles import bundles

    bundles.link(root, user=True)
    return root


def _head(path: Path) -> str:
    return _git(path, "rev-parse", "HEAD").strip()


def _pair(world, user) -> str:
    return f"{_head(world['bundle'])}:{_head(user)}"


@pytest.mark.usefixtures("recorded_assets")
def test_the_stamp_and_the_swarm_revision_carry_both_bundle_commits(world, overlays, user):
    from scripts.profiles import render
    from scripts.swarm import overlays as swarm_overlays

    out = render.render("claude", "rb-eng", overlays=["ov-a"])

    stamp = json.loads((out / render.STAMP).read_text())
    assert stamp["bundle_commit"] == _head(world["bundle"])
    assert stamp["user_bundle_commit"] == _head(user)
    assert swarm_overlays.revision() == _pair(world, user)


def test_the_swarm_revision_without_a_user_bundle_stays_the_root_commit(world, overlays):
    from scripts.swarm import overlays as swarm_overlays

    assert swarm_overlays.revision() == _head(world["bundle"])


@pytest.mark.usefixtures("recorded_assets")
def test_a_pinned_render_uses_the_recorded_user_content_after_the_user_bundle_advances(world, overlays, user):
    from scripts.profiles import bundle_copy, render

    recorded = _pair(world, user)
    _write(user / ".claude" / "CLAUDE.md", "USER DIRECTIVE v2\n")
    _write(user / "profiles" / "ov-user" / ".claude" / "rules" / "ov-user.md", "OV-USER RULE v2\n")
    _commit(user, "moved")

    out = render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)

    persona = (out / "CLAUDE.md").read_text()
    assert "USER DIRECTIVE v1" in persona
    assert "USER DIRECTIVE v2" not in persona
    stamp = json.loads((out / render.STAMP).read_text())
    assert stamp["user_bundle_commit"] == recorded.split(":")[1]
    assert (out / "skills" / "personal" / "SKILL.md").resolve().is_relative_to(out.parent)
    assert bundle_copy.current() is None and bundle_copy.current_user() is None
    assert not list(render.rendered_root().glob("bundle-*"))


@pytest.mark.usefixtures("recorded_assets")
def test_a_pinned_render_ignores_uncommitted_user_bundle_edits(world, overlays, user):
    from scripts.profiles import render

    recorded = _pair(world, user)
    _write(user / ".claude" / "CLAUDE.md", "UNCOMMITTED EDIT\n")
    status = _git(user, "status", "--porcelain")

    out = render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)

    assert "USER DIRECTIVE v1" in (out / "CLAUDE.md").read_text()
    assert "UNCOMMITTED EDIT" not in (out / "CLAUDE.md").read_text()
    assert _git(user, "status", "--porcelain") == status


@pytest.mark.usefixtures("recorded_assets")
def test_a_pinned_render_wears_an_overlay_that_lives_in_the_recorded_user_bundle(world, overlays, user):
    from scripts.profiles import render

    recorded = _pair(world, user)
    _write(user / "profiles" / "ov-user" / ".claude" / "rules" / "ov-user.md", "OV-USER RULE v2\n")
    _commit(user, "moved")

    out = render.render("claude", "rb-eng", overlays=["ov-user"], bundle_revision=recorded)

    assert "OV-USER RULE v1" in (out / "CLAUDE.md").read_text()
    assert "OV-USER RULE v2" not in (out / "CLAUDE.md").read_text()


@pytest.mark.usefixtures("recorded_assets")
def test_a_pinned_render_refuses_a_root_only_revision_while_a_user_bundle_is_linked(world, overlays, user):
    from scripts.profiles import render

    with pytest.raises(ValueError) as refused:
        render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=_head(world["bundle"]))

    assert "recorded bundle commit" in str(refused.value)
    assert "no user bundle commit" in str(refused.value)
    assert render.profile_dir("rb-eng", ["ov-a"]) is None


@pytest.mark.usefixtures("recorded_assets")
def test_a_pinned_render_refuses_a_recorded_user_commit_without_a_linked_user_bundle(world, overlays):
    from scripts.profiles import render

    with pytest.raises(ValueError) as refused:
        render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=f"{_head(world['bundle'])}:abc123")

    assert str(refused.value) == "the launch recorded user bundle commit abc123, but no user bundle is linked"


@pytest.mark.usefixtures("recorded_assets")
def test_a_pinned_render_refuses_an_unreachable_recorded_user_commit_and_publishes_no_home(world, overlays, user):
    from scripts.profiles import bundle_copy, render

    with pytest.raises(ValueError) as refused:
        render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=f"{_head(world['bundle'])}:not-a-commit")

    assert str(refused.value).startswith("the launch recorded user bundle commit not-a-commit, but git cannot read")
    assert render.profile_dir("rb-eng", ["ov-a"]) is None
    assert bundle_copy.current() is None and bundle_copy.current_user() is None


@pytest.mark.usefixtures("recorded_assets")
def test_a_pinned_render_is_reused_until_the_retained_user_copy_goes_dirty(world, overlays, user):
    from scripts.profiles import render

    recorded = _pair(world, user)
    first = render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)
    assert render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=recorded) is None

    copy = first.parent / "user-bundle" / user.name
    (copy / ".claude" / "CLAUDE.md").write_text("DIRTY\n")
    repaired = render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)

    assert repaired is not None
    assert "USER DIRECTIVE v1" in (repaired / "CLAUDE.md").read_text()
    assert _git(user, "status", "--porcelain") == ""


@pytest.mark.usefixtures("recorded_assets")
def test_a_root_only_pinned_render_behaves_as_before(world, overlays):
    from scripts.profiles import bundle_copy, render

    recorded = _head(world["bundle"])

    out = render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)

    stamp = json.loads((out / render.STAMP).read_text())
    assert stamp["bundle_commit"] == recorded
    assert "user_bundle_commit" not in stamp
    assert not (out.parent / "user-bundle").exists()
    assert bundle_copy.current_user() is None
    assert render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=recorded) is None


@pytest.mark.usefixtures("recorded_assets")
def test_a_pinned_codex_render_keeps_the_user_bundle_skills_in_its_own_home(world, overlays, user):
    from scripts.profiles import render

    _write(user / ".codex" / "skills" / "user-codex" / "SKILL.md", "USER CODEX SKILL\n")
    _commit(user, "codex skill")
    recorded = _pair(world, user)
    first = render.render("codex", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)
    config = world["home"] / ".codex" / "config.toml"
    config.write_text(config.read_text() + "\nmodel = 'updated'\n")

    rebuilt = render.render("codex", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)

    skill = rebuilt / "skills" / "user-codex"
    assert skill.resolve().is_relative_to(rebuilt.parent)
    shutil.rmtree(first.parent)
    assert (skill / "SKILL.md").read_text() == "USER CODEX SKILL\n"


def test_bundle_copy_pairs_are_independent_slots(world, user, tmp_path):
    from scripts.profiles import bundle_copy

    root_commit, user_commit = _head(world["bundle"]), _head(user)
    with bundle_copy.recorded(world["bundle"], root_commit, tmp_path / "work", user, user_commit):
        assert bundle_copy.current() is not None
        assert bundle_copy.current_user() is not None
        assert bundle_copy.identity(bundle_copy.current_user() / "x") == user / "x"
        assert bundle_copy.identity(bundle_copy.current() / "x") == world["bundle"] / "x"
    with bundle_copy.recorded(world["bundle"], root_commit, tmp_path / "work"):
        assert bundle_copy.current_user() is None
    assert bundle_copy.current() is None and bundle_copy.current_user() is None


def test_the_scratch_render_accepts_a_user_bundle_beside_the_bundle(world, user, tmp_path, monkeypatch):
    import argparse

    from scripts.profiles import render

    seen = {}
    elsewhere = tmp_path / "other-user"
    elsewhere.mkdir()
    monkeypatch.setattr(
        render.subprocess, "run", lambda argv, env, cwd: seen.update(env=env) or type("D", (), {"returncode": 0})()
    )
    args = argparse.Namespace(
        bundle=None,
        user_bundle=elsewhere,
        out=tmp_path / "scratch",
        name="rb-eng",
        target="claude",
        overlay=[],
        force=False,
    )
    (tmp_path / "scratch").mkdir()

    assert render._render_scratch(args) == 0

    assert seen["env"]["AGENTIHOOKS_USER_BUNDLE_PATH"] == str(elsewhere.resolve())
    assert seen["env"]["AGENTIHOOKS_BUNDLE_PATH"] == str(world["bundle"].resolve())


def test_a_scratch_render_without_a_user_bundle_carries_none(world, tmp_path, monkeypatch):
    import argparse

    from scripts.profiles import render

    seen = {}
    monkeypatch.delenv("AGENTIHOOKS_USER_BUNDLE_PATH", raising=False)
    monkeypatch.setattr(
        render.subprocess, "run", lambda argv, env, cwd: seen.update(env=env) or type("D", (), {"returncode": 0})()
    )
    args = argparse.Namespace(
        bundle=None, user_bundle=None, out=tmp_path / "scratch", name="rb-eng", target="claude", overlay=[], force=False
    )
    (tmp_path / "scratch").mkdir()

    assert render._render_scratch(args) == 0

    assert "AGENTIHOOKS_USER_BUNDLE_PATH" not in seen["env"]
