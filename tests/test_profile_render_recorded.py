import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests import test_profile_render as base

_write, _git, _commit = base._write, base._git, base._commit
world, overlays, recorded_assets = base.world, base.overlays, base.recorded_assets


@pytest.mark.parametrize("target", ["claude", "codex"])
@pytest.mark.usefixtures("recorded_assets")
def test_an_overlay_render_uses_the_recorded_commit_after_the_bundle_advances(world, overlays, target):
    from scripts.profiles import bundle_copy, render

    recorded = _git(world["bundle"], "rev-parse", "HEAD").strip()
    _write(overlays / "ov-a" / ".claude" / "rules" / "ov-a.md", "ADVANCED RULE\n")
    _commit(world["bundle"], "moved")
    head = _git(world["bundle"], "rev-parse", "HEAD").strip()

    out = render.render(target, "rb-eng", overlays=["ov-a"], bundle_revision=recorded)

    persona = out / ("AGENTS.md" if target == "codex" else "CLAUDE.md")
    assert "OV-A RULE MARKER" in persona.read_text()
    assert "ADVANCED RULE" not in persona.read_text()
    stamp = json.loads((out / render.STAMP).read_text())
    assert stamp.get("render", stamp)["bundle_commit"] == recorded
    assert _git(world["bundle"], "rev-parse", "HEAD").strip() == head
    assert _git(world["bundle"], "status", "--porcelain") == ""
    assert (out / "skills" / "bundle-skill" / "SKILL.md").read_text() == "---\nname: bundle-skill\n---\n"
    assert render.binding.inspect(out, "rb-eng", target)["profile"] == "rb-eng"
    assert bundle_copy.current() is None
    assert not list(render.rendered_root().glob("bundle-*"))
    assert _git(out.parent / "bundle" / world["bundle"].name, "rev-parse", "HEAD").strip() == recorded
    assert _git(out.parent / "bundle" / world["bundle"].name, "status", "--porcelain") == ""
    assert render.render(target, "rb-eng", overlays=["ov-a"], bundle_revision=recorded) is None
    assert not list(render.rendered_root().glob("bundle-*"))


@pytest.mark.parametrize(("target", "persona"), [("claude", "CLAUDE.md"), ("codex", "AGENTS.md")])
@pytest.mark.usefixtures("recorded_assets")
def test_an_overlay_render_ignores_uncommitted_bundle_changes(world, overlays, target, persona):
    from scripts.profiles import render

    recorded = _git(world["bundle"], "rev-parse", "HEAD").strip()
    rule = overlays / "ov-a" / ".claude" / "rules" / "ov-a.md"
    _write(rule, "EDITED RULE\n")
    status = _git(world["bundle"], "status", "--porcelain")

    out = render.render(target, "rb-eng", overlays=["ov-a"], bundle_revision=recorded)

    assert "OV-A RULE MARKER" in (out / persona).read_text()
    assert "EDITED RULE" not in (out / persona).read_text()
    assert rule.read_text() == "EDITED RULE\n"
    assert _git(world["bundle"], "status", "--porcelain") == status
    assert world["install"]._get_bundle_path() == world["bundle"]


@pytest.mark.usefixtures("recorded_assets")
def test_an_overlay_render_refuses_a_recorded_commit_without_a_linked_bundle(world, overlays, monkeypatch):
    from scripts.profiles import render

    monkeypatch.setattr(render, "_bundle", lambda: None)

    with pytest.raises(ValueError) as refused:
        render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision="abc123")

    assert str(refused.value) == "the launch recorded bundle commit abc123, but no bundle is linked"


@pytest.mark.usefixtures("recorded_assets")
def test_an_overlay_render_refuses_a_bundle_git_cannot_read(world, overlays, monkeypatch, tmp_path):
    from scripts.profiles import render

    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr(render, "_bundle", lambda: plain)

    with pytest.raises(ValueError) as refused:
        render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision="abc123")

    assert str(refused.value).startswith(
        f"the launch recorded bundle commit abc123, but git cannot read the bundle at {plain}: fatal: not a git repository"
    )


@pytest.mark.usefixtures("recorded_assets")
def test_an_overlay_render_refuses_a_bundle_git_does_not_answer(world, overlays, monkeypatch):
    from scripts.profiles import render

    def hang(argv, **kwargs):
        assert kwargs["timeout"] == 10
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(render.subprocess, "run", hang)

    with pytest.raises(ValueError) as refused:
        render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision="abc123")

    assert str(refused.value) == (
        f"the launch recorded bundle commit abc123, but git did not answer within 10 seconds for the bundle at "
        f"{world['bundle']}"
    )


@pytest.mark.usefixtures("recorded_assets")
def test_an_unavailable_recorded_commit_does_not_publish_a_home(world, overlays):
    from scripts.profiles import bundle_copy, render

    with pytest.raises(ValueError) as refused:
        render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision="not-a-commit")

    assert str(refused.value).startswith("the launch recorded bundle commit not-a-commit, but git cannot read")
    assert render.profile_dir("rb-eng", ["ov-a"]) is None
    assert bundle_copy.current() is None
    assert world["install"]._get_bundle_path() == world["bundle"]


@pytest.mark.usefixtures("recorded_assets")
def test_an_overlay_render_from_the_recorded_commit_renders_and_stamps_it(world, overlays):
    from scripts.profiles import render

    recorded = _git(world["bundle"], "rev-parse", "HEAD").strip()

    out = render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)

    assert "OV-A RULE MARKER" in (out / "CLAUDE.md").read_text()
    assert json.loads((out / render.STAMP).read_text())["bundle_commit"] == recorded
    assert render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=recorded) is None


@pytest.mark.usefixtures("recorded_assets")
def test_pinned_codex_rebuild_keeps_bundle_links_in_its_new_home(world, overlays):
    from scripts.profiles import bundle_copy, render

    _write(overlays / "ov-a" / ".codex" / "skills" / "pinned-skill" / "SKILL.md", "PINNED SKILL\n")
    _write(world["bundle"] / ".codex" / "skills" / "root-skill" / "SKILL.md", "ROOT SKILL\n")
    _commit(world["bundle"], "native skill")
    recorded = _git(world["bundle"], "rev-parse", "HEAD").strip()
    first = render.render("codex", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)
    config = world["home"] / ".codex" / "config.toml"
    config.write_text(config.read_text() + "\nmodel = 'updated'\n")

    rebuilt = render.render("codex", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)

    assert rebuilt.parent != first.parent
    skill = rebuilt / "skills" / "pinned-skill"
    bundled = rebuilt / "skills" / "bundle-skill"
    assert skill.resolve().is_relative_to(rebuilt.parent)
    assert bundled.resolve().is_relative_to(rebuilt.parent)
    shutil.rmtree(first.parent)
    assert (skill / "SKILL.md").read_text() == "PINNED SKILL\n"
    assert (bundled / "SKILL.md").read_text() == "---\nname: bundle-skill\n---\n"
    assert (rebuilt / "skills" / "root-skill" / "SKILL.md").read_text() == "ROOT SKILL\n"
    assert bundle_copy.current() is None
    assert render.render("codex", "rb-eng", overlays=["ov-a"], bundle_revision=recorded) is None


@pytest.mark.usefixtures("recorded_assets")
def test_a_pinned_codex_render_keeps_its_isolation(world, overlays):
    from scripts.profiles import render

    recorded = _git(world["bundle"], "rev-parse", "HEAD").strip()

    out = render.render("codex", "rb-eng", overlays=["ov-a"], bundle_revision=recorded, isolate=True)

    assert json.loads((out / render.STAMP).read_text())["isolate"] is True


@pytest.mark.usefixtures("recorded_assets")
def test_an_unpinned_render_reuses_its_home(world, overlays):
    from scripts.profiles import render

    first = render.render("claude", "rb-eng", overlays=["ov-a"])

    assert first is not None
    assert render.render("claude", "rb-eng", overlays=["ov-a"]) is None


@pytest.mark.usefixtures("recorded_assets")
def test_pinned_render_replaces_a_dirty_retained_bundle_copy(world, overlays):
    from scripts.profiles import bundle_copy, render

    recorded = _git(world["bundle"], "rev-parse", "HEAD").strip()
    first = render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)
    skill = first / "skills" / "bundle-skill" / "SKILL.md"
    skill.write_text("DIRTY SKILL\n")

    repaired = render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)

    assert repaired is not None
    assert repaired.parent != first.parent
    assert (repaired / "skills" / "bundle-skill" / "SKILL.md").read_text() == "---\nname: bundle-skill\n---\n"
    assert _git(world["bundle"], "status", "--porcelain") == ""
    assert bundle_copy.current() is None


@pytest.mark.usefixtures("recorded_assets")
def test_pinned_render_preserves_bundle_symlinks(world, overlays):
    from scripts.profiles import bundle_copy, render

    skill = world["bundle"] / ".claude" / "skills" / "bundle-skill"
    (skill / "linked.md").symlink_to("SKILL.md")
    _commit(world["bundle"], "linked skill")
    recorded = _git(world["bundle"], "rev-parse", "HEAD").strip()

    out = render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)

    linked = out / "skills" / "bundle-skill" / "linked.md"
    assert linked.is_symlink()
    assert linked.readlink() == Path("SKILL.md")
    assert linked.read_text() == "---\nname: bundle-skill\n---\n"
    assert bundle_copy.current() is None


@pytest.mark.usefixtures("recorded_assets")
def test_pinned_render_repairs_a_home_missing_its_bundle_copy(world, overlays):
    from scripts.profiles import bundle_copy, render

    recorded = _git(world["bundle"], "rev-parse", "HEAD").strip()
    first = render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)
    shutil.rmtree(first.parent / "bundle")

    repaired = render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)

    assert repaired is not None
    assert (repaired / "skills" / "bundle-skill" / "SKILL.md").is_file()
    assert bundle_copy.current() is None


@pytest.mark.parametrize("failure", ["returncode", "timeout"])
@pytest.mark.parametrize("operation", ["rev-parse", "status"])
@pytest.mark.usefixtures("recorded_assets")
def test_an_unreadable_cached_copy_renders_a_fresh_home(world, overlays, monkeypatch, failure, operation):
    from scripts.profiles import bundle_copy, render

    recorded = _git(world["bundle"], "rev-parse", "HEAD").strip()
    first = render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)
    copied = first.parent / "bundle" / world["bundle"].name
    run = subprocess.run

    def fail_copy(argv, **kwargs):
        if argv[:3] == ["git", "-C", str(copied)] and argv[3] == operation:
            assert kwargs["timeout"] == 10
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 10)
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="copy failure\n")
        return run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_copy)

    repaired = render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)

    assert repaired is not None
    assert repaired.parent != first.parent
    assert _git(repaired.parent / "bundle" / world["bundle"].name, "rev-parse", "HEAD").strip() == recorded
    assert bundle_copy.current() is None


@pytest.mark.parametrize("step", ["init", "fetch", "update-ref", "read-tree", "checkout-index"])
@pytest.mark.usefixtures("recorded_assets")
def test_a_slow_copy_step_waits_longer_than_a_bundle_read(world, overlays, monkeypatch, step):
    from scripts.profiles import bundle_copy, render

    recorded = _git(world["bundle"], "rev-parse", "HEAD").strip()
    run = subprocess.run

    def hang(argv, **kwargs):
        if argv[:2] == ["git", "-C"] and argv[3:4] == [step] and argv[2] != str(world["bundle"]):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", hang)

    with pytest.raises(ValueError) as refused:
        render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)

    assert str(refused.value) == (
        f"the launch recorded bundle commit {recorded}, but git did not answer within 300 seconds for the bundle at "
        f"{world['bundle']}"
    )
    assert render.profile_dir("rb-eng", ["ov-a"]) is None
    assert bundle_copy.current() is None


@pytest.mark.usefixtures("recorded_assets")
def test_pinned_copilot_rebuild_keeps_the_recorded_overlay_servers(world, overlays, monkeypatch):
    from scripts.profiles import bundle_copy, render

    server = {"mcpServers": {"overlay-server": {"command": "overlay-command"}}}
    _write(overlays / "ov-a" / ".copilot" / "mcp-config.overrides.json", json.dumps(server))
    _commit(world["bundle"], "overlay server")
    recorded = _git(world["bundle"], "rev-parse", "HEAD").strip()
    first = render.render("copilot", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)
    (first / "mcp-config.json").write_text("{}")

    rebuilt = render.render("copilot", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)

    assert rebuilt.parent != first.parent
    servers = json.loads((rebuilt / "mcp-config.json").read_text())["mcpServers"]
    assert servers["overlay-server"]["command"] == "overlay-command"
    assert servers["bundle-srv"]["command"] == "bundle-server"
    assert bundle_copy.current() is None


@pytest.mark.usefixtures("recorded_assets")
@pytest.mark.parametrize("source", ["linked", "package"])
def test_pinned_render_keeps_external_and_package_parents(world, overlays, tmp_path, source):
    from scripts.profiles import bundle_copy, render

    parent = "library" if source == "linked" else "engineer"
    library = tmp_path / ("library" if source == "linked" else "package-roles/engineer")
    _write(library / "profile.yml", f"name: {parent}\n")
    _write(library / "CLAUDE.md", "LIBRARY PERSONA\n")
    if source == "linked":
        state = world["install"]._load_state()
        state["linked_profiles"] = [{"name": "library", "path": str(library)}]
        world["install"]._save_state(state)
    _write(overlays / "rb-eng" / "profile.yml", f"name: rb-eng\nextends: [{parent}, package:engineer]\n")
    _commit(world["bundle"], "library parent")
    recorded = _git(world["bundle"], "rev-parse", "HEAD").strip()

    out = render.render("claude", "rb-eng", overlays=["ov-a"], bundle_revision=recorded)

    assert "LIBRARY PERSONA" in (out / "CLAUDE.md").read_text()
    assert bundle_copy.current() is None


@pytest.mark.usefixtures("recorded_assets")
@pytest.mark.parametrize("target", ["claude", "codex"])
def test_pinned_render_honors_force(world, overlays, target):
    from scripts.profiles import bundle_copy, render

    recorded = _git(world["bundle"], "rev-parse", "HEAD").strip()
    first = render.render(target, "rb-eng", overlays=["ov-a"], bundle_revision=recorded)

    forced = render.render(target, "rb-eng", force=True, overlays=["ov-a"], bundle_revision=recorded)

    assert forced is not None
    assert forced.parent != first.parent
    assert bundle_copy.current() is None
