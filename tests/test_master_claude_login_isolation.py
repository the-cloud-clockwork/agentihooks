import json

import pytest

from scripts import master_claude_login as login
from scripts.profiles import render
from tests.test_profile_render import _write
from tests.test_profile_render import world as world

ROLES = ("master", "engineer", "cicd", "planner")


@pytest.fixture
def roles(world, monkeypatch, tmp_path):
    monkeypatch.setattr(login.hooks_config, "AGENTIHOOKS_HOME", str(tmp_path / "ah"))
    for name in ROLES:
        _write(world["bundle"] / "profiles" / name / "profile.yml", f"name: {name}\n")
    _write(world["home"] / ".claude" / ".credentials.json", "operator login")
    return world


def _homes():
    for name in ROLES:
        render.render_claude(name)
    return {name: render.profile_dir(name) / "claude" for name in ROLES}


def test_the_master_login_home_never_touches_the_shared_credential_link(roles):
    homes = _homes()
    operator = roles["home"] / ".claude" / ".credentials.json"
    target = login.build(homes["master"])
    (target / login.CREDENTIALS).write_text(json.dumps({login.GRANT: {"refreshToken": "r"}}))
    for name, home in homes.items():
        link = home / ".credentials.json"
        assert link.is_symlink() and link.readlink() == operator, name
    assert operator.read_text() == "operator login"
    assert not (target / login.CREDENTIALS).is_symlink()


def test_no_worker_home_can_reach_the_master_login(roles):
    homes = _homes()
    target = login.build(homes["master"])
    (target / login.CREDENTIALS).write_text("{}")
    master_root = login.root().resolve()
    for name in ("engineer", "cicd", "planner"):
        for path in homes[name].rglob("*"):
            assert master_root not in path.resolve().parents and path.resolve() != master_root, (name, path)


def test_the_master_home_keeps_every_rendered_setting_and_hook(roles):
    homes = _homes()
    target = login.build(homes["master"])
    assert (target / "settings.json").read_bytes() == (homes["master"] / "settings.json").read_bytes()
    assert (target / "CLAUDE.md").read_bytes() == (homes["master"] / "CLAUDE.md").read_bytes()
