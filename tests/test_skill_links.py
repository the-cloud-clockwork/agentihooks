import argparse
import json
import sys
from pathlib import Path

import pytest

from scripts import deps_preflight, install
from tests import test_install as install_tests


@pytest.fixture
def skill_home(tmp_path, monkeypatch):
    profiles = install_tests.TestInstallGlobalHonoursTarget()._tiny_profile(tmp_path)
    monkeypatch.setattr(install, "PROFILES_DIR", profiles)
    monkeypatch.setattr(install, "_get_bundle_path", lambda: None)
    monkeypatch.setattr(install, "_install_cli_tool", lambda: None)
    monkeypatch.setattr(install, "_resolve_hooks_python", lambda: Path(sys.executable))
    package = tmp_path / "package"
    monkeypatch.setattr(install, "PACKAGE_FEATURES_DIR", package)
    source = profiles / "tiny" / ".claude" / "skills" / "worktree"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# worktree\n")
    run_install()
    home = Path.home()
    link = home / ".claude" / "skills" / "worktree"
    (source / "SKILL.md").unlink()
    source.rmdir()
    return home, package, link


def run_install():
    install.install_global(argparse.Namespace(profile="tiny", settings_profile="", install_target="claude"))


def test_install_removes_dangling_skill_when_all_source_directories_disappear(skill_home, tmp_path, monkeypatch):
    _, _, link = skill_home
    link.resolve().parent.rmdir()
    run_install()
    assert not link.is_symlink()


def test_install_relinks_moved_skill_and_preserves_personal_files(skill_home, tmp_path, monkeypatch):
    home, package, link = skill_home
    current = package / "skills" / "worktree"
    current.mkdir(parents=True)
    (current / "SKILL.md").write_text("# moved worktree\n")
    personal = home / ".claude" / "skills" / "personal"
    personal.symlink_to(tmp_path / "unmounted")
    note = personal.parent / "notes.txt"
    note.write_text("personal")
    run_install()
    assert link.resolve() == current
    assert (link / "SKILL.md").read_text() == "# moved worktree\n"
    assert personal.is_symlink()
    assert note.read_text() == "personal"


def test_deps_check_reports_dangling_managed_skill_without_manifest(skill_home, capsys):
    _, _, link = skill_home
    capsys.readouterr()
    assert deps_preflight.main(["check"]) == 1
    assert capsys.readouterr().out == f"missing: skill:worktree (skill) — dangling link: {link}\ndeps: 1 unsatisfied\n"
    assert link.is_symlink()


def test_deps_check_ignores_personal_dangling_links(tmp_path, capsys):
    skills = install.CLAUDE_HOME / "skills"
    skills.mkdir()
    personal = skills / "personal"
    personal.symlink_to(tmp_path / "unmounted")
    assert deps_preflight.main(["check"]) == 0
    assert capsys.readouterr().out == "deps: all satisfied\n"
    assert personal.is_symlink()


@pytest.mark.parametrize("manifest", [False, True])
def test_deps_ensure_repairs_moved_skill_before_cached_checks(skill_home, tmp_path, monkeypatch, capsys, manifest):
    home, package, link = skill_home
    current = package / "skills" / "worktree"
    current.mkdir(parents=True)
    (current / "SKILL.md").write_text("# moved worktree\n")
    old = link.resolve()
    if manifest:
        path = tmp_path / "deps.json"
        path.write_text('{"deps": []}')
        monkeypatch.setattr(deps_preflight, "manifest_path", lambda: path)
        old.mkdir()
        (old / "SKILL.md").write_text("# old\n")
        assert deps_preflight.ensure(quiet=True) == 0
        assert deps_preflight.stamp_fresh(path)
        (old / "SKILL.md").unlink()
        old.rmdir()
    capsys.readouterr()
    assert deps_preflight.main(["ensure", "--quiet"]) == 0
    assert link.resolve() == current
    assert (link / "SKILL.md").read_text() == "# moved worktree\n"
    assert deps_preflight.main(["check"]) == 0
    assert capsys.readouterr().out == (
        f"\x1b[32m  [OK] Re-linked skill 'worktree' → {current}\x1b[0m\ndeps: all satisfied\n"
    )
    assert home == Path.home()


def test_deps_ensure_removes_obsolete_skill_and_keeps_personal_links(skill_home, tmp_path):
    _, _, link = skill_home
    personal = link.parent / "personal"
    personal.symlink_to(tmp_path / "unmounted")
    assert deps_preflight.ensure(quiet=True) == 0
    assert not link.is_symlink()
    assert personal.is_symlink()
    assert str(link) not in install._state_links()


def test_deps_ensure_follows_bundle_move_and_later_profile_precedence(skill_home, tmp_path, monkeypatch):
    _, package, link = skill_home
    bundle = tmp_path / "bundle"
    global_skill = bundle / ".claude" / "skills" / "worktree"
    global_skill.mkdir(parents=True)
    (global_skill / "SKILL.md").write_text("# global\n")
    packaged = package / "skills" / "worktree"
    packaged.mkdir(parents=True)
    (packaged / "SKILL.md").write_text("# packaged\n")
    later = install.PROFILES_DIR / "later" / ".claude" / "skills" / "worktree"
    later.mkdir(parents=True)
    (later / "SKILL.md").write_text("# later\n")
    monkeypatch.setattr(install, "_get_bundle_path", lambda: bundle)
    state = install._load_state()
    state["bundle"] = {"path": str(bundle)}
    state["managed_links"] = []
    install._save_state(state)
    old = bundle / "profiles" / "kit" / ".claude" / "skills" / "worktree"
    link.unlink()
    link.symlink_to(old)
    assert deps_preflight.main(["check"]) == 1
    assert deps_preflight.ensure(quiet=True) == 0
    assert link.resolve() == global_skill
    state = install._load_state()
    state["targets"]["global"]["claude"]["profile"] = "tiny,later"
    install._save_state(state)
    link.unlink()
    link.symlink_to(old)
    assert deps_preflight.ensure(quiet=True) == 0
    assert link.resolve() == later


def test_deps_check_and_ensure_cover_operator_home_from_rendered_session(skill_home, tmp_path, monkeypatch):
    _, package, link = skill_home
    current = package / "skills" / "worktree"
    current.mkdir(parents=True)
    (current / "SKILL.md").write_text("# repaired\n")
    rendered = tmp_path / "rendered" / "claude"
    rendered.mkdir(parents=True)
    monkeypatch.setattr(install, "CLAUDE_HOME", rendered)
    assert deps_preflight.main(["check"]) == 1
    assert deps_preflight.ensure(quiet=True) == 0
    assert link.resolve() == current


def test_deps_check_reports_skills_and_missing_tools_together(skill_home, tmp_path, monkeypatch, capsys):
    _, _, link = skill_home
    path = tmp_path / "deps.json"
    path.write_text(
        json.dumps(
            {
                "deps": [
                    {"id": "missing-tool", "kind": "system", "check": [sys.executable, "-c", "raise SystemExit(1)"]},
                    {"id": "present-tool", "kind": "system", "check": [sys.executable, "-c", "pass"]},
                ]
            }
        )
    )
    monkeypatch.setattr(deps_preflight, "manifest_path", lambda: path)
    capsys.readouterr()
    assert deps_preflight.main(["check"]) == 1
    assert capsys.readouterr().out == (
        f"missing: skill:worktree (skill) — dangling link: {link}\n"
        "missing: missing-tool (system)\ndeps: 2 unsatisfied\n"
    )


def test_deps_ensure_can_repair_a_second_move(skill_home, tmp_path):
    _, package, link = skill_home
    first = package / "skills" / "worktree"
    first.mkdir(parents=True)
    (first / "SKILL.md").write_text("# first\n")
    assert deps_preflight.ensure(quiet=True) == 0
    (first / "SKILL.md").unlink()
    first.rmdir()
    second = install.PROFILES_DIR / "tiny" / ".claude" / "skills" / "worktree"
    second.mkdir()
    (second / "SKILL.md").write_text("# second\n")
    assert deps_preflight.main(["check"]) == 1
    assert deps_preflight.ensure(quiet=True) == 0
    assert link.resolve() == second


def test_deps_ensure_uses_default_profile_without_install_record(skill_home):
    _, _, link = skill_home
    current = install.PROFILES_DIR / "default" / ".claude" / "skills" / "worktree"
    current.mkdir(parents=True)
    (current / "SKILL.md").write_text("# default\n")
    state = install._load_state()
    del state["targets"]
    install._save_state(state)
    assert deps_preflight.ensure(quiet=True) == 0
    assert link.resolve() == current


@pytest.mark.parametrize("profile", [None, "later"])
def test_deps_ensure_respects_rendered_profile_separately_from_operator_profile(
    skill_home, tmp_path, monkeypatch, profile
):
    _, _, operator_link = skill_home
    tiny = operator_link.resolve()
    tiny.mkdir()
    (tiny / "SKILL.md").write_text("# tiny\n")
    later = install.PROFILES_DIR / "later" / ".claude" / "skills" / "worktree"
    later.mkdir(parents=True)
    (later / "SKILL.md").write_text("# later\n")
    rendered = tmp_path / "rendered" / "claude"
    (rendered / "skills").mkdir(parents=True)
    rendered_link = rendered / "skills" / "worktree"
    old = tmp_path / "removed"
    rendered_link.symlink_to(old)
    install._state_record_link(rendered_link, old, "skills")
    monkeypatch.setattr(install, "CLAUDE_HOME", rendered)
    if profile:
        monkeypatch.setenv("AGENTIHOOKS_PROFILE", profile)
    operator_link.unlink()
    operator_link.symlink_to(old)
    install._state_record_link(operator_link, old, "skills")
    assert deps_preflight.ensure(quiet=True) == 0
    assert rendered_link.resolve() == (later if profile else tiny)
    assert operator_link.resolve() == tiny
