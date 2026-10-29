import argparse
import json
import sys

import pytest

from hooks.context import profile_chain
from scripts import install
from scripts.profiles import bundles


@pytest.fixture
def trees(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    root = tmp_path / "root"
    user = tmp_path / "user"
    for folder in (builtin, root / "profiles", user / "profiles"):
        folder.mkdir(parents=True)
    monkeypatch.setattr(profile_chain, "BUILT_IN_PROFILES", builtin)
    monkeypatch.setattr(install, "PROFILES_DIR", builtin)
    return builtin, root, user


def _state() -> dict:
    return json.loads(install.STATE_JSON.read_text())


def test_linking_a_user_bundle_keeps_the_root_link(trees):
    _, root, user = trees
    bundles.link(root)
    bundles.link(user, user=True)

    state = _state()
    assert state["bundle"]["path"] == str(root)
    assert state["user_bundle"]["path"] == str(user)
    assert profile_chain.bundle_roots(state) == [("root", root), ("user", user)]


def test_unlinking_the_user_bundle_keeps_the_root_and_its_files(trees):
    _, root, user = trees
    (user / "profiles" / "mine").mkdir()
    bundles.link(root)
    bundles.link(user, user=True)

    bundles.unlink(user=True)

    assert _state()["bundle"]["path"] == str(root)
    assert "user_bundle" not in _state()
    assert (user / "profiles" / "mine").is_dir()
    assert root.is_dir()


def test_unlinking_the_root_keeps_the_user_bundle(trees):
    _, root, user = trees
    bundles.link(root)
    bundles.link(user, user=True)

    bundles.unlink()

    assert "bundle" not in _state()
    assert _state()["user_bundle"]["path"] == str(user)


def test_the_same_directory_cannot_be_both_bundles(trees, capsys):
    _, root, _ = trees
    bundles.link(root)

    with pytest.raises(SystemExit):
        bundles.link(root, user=True)

    assert "already linked as the root bundle" in capsys.readouterr().err
    assert "user_bundle" not in _state()


def test_a_machine_without_a_user_bundle_resolves_as_before(trees):
    _, root, _ = trees
    bundles.link(root)
    state = _state()

    assert profile_chain.bundle_roots(state) == [("root", root)]
    assert profile_chain.user_bundle_path(state) is None
    assert profile_chain.missing_bundle_roots(state) == []
    assert install._get_user_bundle_path() is None
    assert "user_bundle" not in state


def test_a_configured_user_path_that_is_gone_is_reported_and_not_applied(trees, capsys):
    _, root, user = trees
    bundles.link(root)
    bundles.link(user, user=True)
    user.rename(user.with_name("moved"))
    state = _state()

    assert profile_chain.bundle_roots(state) == [("root", root)]
    assert profile_chain.missing_bundle_roots(state) == [("user", user)]
    bundles.warn_unavailable()
    assert f"Configured user bundle {user} is missing - not applied." in capsys.readouterr().out


def test_the_environment_names_a_user_bundle_when_none_is_linked(trees, monkeypatch):
    _, _, user = trees
    monkeypatch.setenv("AGENTIHOOKS_USER_BUNDLE_PATH", str(user))

    assert install._get_user_bundle_path() == user.resolve()
    assert install._under_managed_source(str(user / ".claude" / "skills" / "mine"))


def test_profiles_resolve_builtin_then_root_then_user_then_linked_then_package_role(trees):
    builtin, root, user = trees
    linked = root.parent / "linked"
    linked.mkdir()
    for folder in (builtin / "a", root / "profiles" / "a", user / "profiles" / "a", user / "profiles" / "b"):
        folder.mkdir(parents=True)
    (user / "profiles" / "c").mkdir()
    links = {"b": linked, "c": linked}
    roles = profile_chain.PACKAGE_ROLES

    assert profile_chain.profile_candidates(root, "a", links, user) == [
        ("a", [builtin / "a", root / "profiles" / "a", user / "profiles" / "a", roles / "a"])
    ]
    assert profile_chain.profile_dirs(root, "a,b,c", links, user) == [
        ("a", builtin / "a"),
        ("b", user / "profiles" / "b"),
        ("c", user / "profiles" / "c"),
    ]
    assert profile_chain.profile_dirs(root, "b", links) == [("b", linked)]


def test_the_installer_resolves_a_user_profile_and_labels_its_source(trees):
    _, root, user = trees
    (user / "profiles" / "mine").mkdir()
    (root / "profiles" / "shared").mkdir()
    bundles.link(root)
    bundles.link(user, user=True)

    assert install._resolve_profile_dir("mine") == user / "profiles" / "mine"
    assert install._profile_source_label("mine") == "user bundle"
    assert install._profile_source_label("shared") == "bundle"
    assert install._resolve_profile_chain("shared,mine") == [
        ("shared", root / "profiles" / "shared"),
        ("mine", user / "profiles" / "mine"),
    ]
    assert {"mine", "shared"} <= set(install._available_profiles())


def test_a_profile_in_both_bundles_resolves_to_the_root_and_warns(trees, capsys):
    _, root, user = trees
    (root / "profiles" / "same").mkdir()
    (user / "profiles" / "same").mkdir()
    bundles.link(root)
    bundles.link(user, user=True)

    assert profile_chain.profile_collisions(root, user) == ["same"]
    assert install._resolve_profile_dir("same") == root / "profiles" / "same"
    bundles.warn_unavailable()
    assert "Profile 'same' exists in the root and the user bundle - the root's is used." in capsys.readouterr().out


def test_a_linked_profile_cannot_take_a_user_bundle_profile_name(trees, capsys):
    _, root, user = trees
    (user / "profiles" / "mine").mkdir()
    bundles.link(user, user=True)
    elsewhere = root.parent / "elsewhere"
    elsewhere.mkdir()

    with pytest.raises(SystemExit):
        install._link_profile_link(elsewhere, name="mine", run_init=False)

    assert "collides with a bundle profile" in capsys.readouterr().err


def test_bundle_commands_take_user_on_the_command_line(trees, monkeypatch, capsys):
    _, root, user = trees
    bundles.link(root)

    monkeypatch.setattr(sys, "argv", ["agentihooks", "bundle", "link", "--user", str(user)])
    install.main()
    assert _state()["user_bundle"]["path"] == str(user)

    monkeypatch.setattr(sys, "argv", ["agentihooks", "bundle", "list", "--user"])
    install.main()
    assert f"User bundle: {user}" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["agentihooks", "bundle", "unlink", "--user"])
    install.main()
    assert "user_bundle" not in _state()
    assert _state()["bundle"]["path"] == str(root)


def _init(monkeypatch, **options):
    monkeypatch.setattr(install, "install_global", lambda args: None)
    monkeypatch.setattr(install, "_rerender_profile_homes", lambda target: None)
    monkeypatch.setattr(install, "_update_bashrc_block", lambda: None)
    namespace = argparse.Namespace(profile="default", init_settings_profile=None, bundle=None, repo=None, **options)
    install.cmd_init_unified(namespace)


def test_init_links_the_user_bundle_beside_the_root(trees, monkeypatch):
    _, root, user = trees
    bundles.link(root)

    _init(monkeypatch, user_bundle=str(user))

    assert _state()["bundle"]["path"] == str(root)
    assert _state()["user_bundle"]["path"] == str(user)


def test_init_refuses_a_user_bundle_directory_that_does_not_exist(trees, monkeypatch, capsys):
    _, root, user = trees
    bundles.link(root)

    with pytest.raises(SystemExit):
        _init(monkeypatch, user_bundle=str(user / "absent"))

    assert "User bundle directory not found" in capsys.readouterr().err
    assert "user_bundle" not in _state()


def test_force_init_keeps_the_root_and_the_user_bundle(trees, monkeypatch):
    _, root, user = trees
    bundles.link(root)
    bundles.link(user, user=True)
    before = _state()
    monkeypatch.setattr(install, "_clean_state_dir", lambda: install.STATE_JSON.unlink())

    _init(monkeypatch, force=True)

    after = _state()
    assert after["bundle"]["path"] == before["bundle"]["path"]
    assert after["user_bundle"]["path"] == before["user_bundle"]["path"]


def test_status_names_the_user_bundle_only_when_one_is_linked(trees, monkeypatch):
    from unittest.mock import patch

    from scripts import status_checker

    _, root, user = trees
    monkeypatch.setattr(status_checker, "STATE_JSON", install.STATE_JSON)
    bundles.link(root)

    def report():
        with patch("hooks._redis.get_redis", return_value=None):
            results = status_checker.run_all_checks()
        return results["profile"], status_checker.format_cli(results)

    plain, text = report()
    assert plain["user_bundle"] == "(none)"
    assert "user bundle" not in text

    bundles.link(user, user=True)
    linked, text = report()
    assert linked["user_bundle"] == str(user)
    assert linked["user_bundle_ok"] is True
    assert f"user bundle: {user}" in text
