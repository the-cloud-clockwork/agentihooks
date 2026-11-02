import json
import sys

import pytest

from hooks.context import profile_chain
from scripts import install
from scripts.profiles import bundles
from tests.test_user_bundle import _init


@pytest.fixture
def trees(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    root = tmp_path / "root"
    shared = tmp_path / "shared"
    user = tmp_path / "user"
    for folder in (builtin, root / "profiles", shared / "profiles", user / "profiles"):
        folder.mkdir(parents=True)
    monkeypatch.setattr(profile_chain, "BUILT_IN_PROFILES", builtin)
    monkeypatch.setattr(install, "PROFILES_DIR", builtin)
    return builtin, root, shared, user


def _state() -> dict:
    return json.loads(install.STATE_JSON.read_text())


def _link_all(root, shared, user) -> None:
    bundles.link(root)
    bundles.link(user, user=True)
    bundles.link(shared, shared=True)


def test_linking_a_shared_bundle_keeps_the_other_two_and_orders_root_shared_user(trees):
    _, root, shared, user = trees
    _link_all(root, shared, user)

    state = _state()
    assert state["bundle"]["path"] == str(root)
    assert state["shared_bundle"]["path"] == str(shared)
    assert state["user_bundle"]["path"] == str(user)
    assert profile_chain.shared_bundle_path(state) == shared
    assert profile_chain.bundle_roots(state) == [("root", root), ("shared", shared), ("user", user)]
    assert install._get_shared_bundle_path() == shared


def test_a_machine_without_a_shared_bundle_resolves_as_before(trees):
    _, root, _, user = trees
    bundles.link(root)
    bundles.link(user, user=True)
    state = _state()

    assert profile_chain.bundle_roots(state) == [("root", root), ("user", user)]
    assert profile_chain.shared_bundle_path(state) is None
    assert profile_chain.missing_bundle_roots(state) == []
    assert install._get_shared_bundle_path() is None
    assert "shared_bundle" not in state


@pytest.mark.parametrize(
    ("first", "second", "holder"),
    [
        ("root", "shared", "root"),
        ("user", "shared", "user"),
        ("shared", "root", "shared"),
        ("shared", "user", "shared"),
    ],
)
def test_one_directory_cannot_fill_two_slots(trees, capsys, first, second, holder):
    _, root, shared, user = trees
    other = root.parent / "same"
    other.mkdir()
    link = {
        "root": lambda path: bundles.link(path),
        "shared": lambda path: bundles.link(path, shared=True),
        "user": lambda path: bundles.link(path, user=True),
    }
    link[first](other)

    with pytest.raises(SystemExit):
        link[second](other)

    assert f"already linked as the {holder} bundle" in capsys.readouterr().err


def test_unlinking_the_shared_bundle_keeps_the_others_and_every_file(trees):
    _, root, shared, user = trees
    (shared / "profiles" / "team").mkdir()
    _link_all(root, shared, user)

    bundles.unlink(shared=True)

    state = _state()
    assert "shared_bundle" not in state
    assert state["bundle"]["path"] == str(root)
    assert state["user_bundle"]["path"] == str(user)
    assert (shared / "profiles" / "team").is_dir()


def test_unlinking_the_root_or_the_user_bundle_keeps_the_shared_one(trees):
    _, root, shared, user = trees
    _link_all(root, shared, user)

    bundles.unlink()
    bundles.unlink(user=True)

    assert _state()["shared_bundle"]["path"] == str(shared)


def test_unlinking_when_none_is_linked_says_so(trees, capsys):
    bundles.unlink(shared=True)

    assert "No shared bundle linked." in capsys.readouterr().out


def test_bundle_commands_take_shared_on_the_command_line(trees, monkeypatch, capsys):
    _, root, shared, _ = trees
    bundles.link(root)

    monkeypatch.setattr(sys, "argv", ["agentihooks", "bundle", "link", "--shared", str(shared)])
    install.main()
    assert _state()["shared_bundle"]["path"] == str(shared)

    monkeypatch.setattr(sys, "argv", ["agentihooks", "bundle", "list", "--shared"])
    install.main()
    assert f"Shared bundle: {shared}" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["agentihooks", "bundle", "unlink", "--shared"])
    install.main()
    assert "shared_bundle" not in _state()
    assert _state()["bundle"]["path"] == str(root)


def test_a_bundle_command_takes_either_shared_or_user_never_both(trees, monkeypatch, capsys):
    _, _, shared, _ = trees
    monkeypatch.setattr(sys, "argv", ["agentihooks", "bundle", "link", "--shared", "--user", str(shared)])

    with pytest.raises(SystemExit) as raised:
        install.main()

    assert raised.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_a_configured_shared_path_that_is_gone_is_reported_and_not_applied(trees, capsys):
    _, root, shared, user = trees
    _link_all(root, shared, user)
    shared.rename(shared.with_name("moved"))
    state = _state()

    assert profile_chain.bundle_roots(state) == [("root", root), ("user", user)]
    assert profile_chain.missing_bundle_roots(state) == [("shared", shared)]
    bundles.warn_unavailable()
    assert f"Configured shared bundle {shared} is missing - not applied." in capsys.readouterr().out


def test_the_environment_names_a_shared_bundle_when_none_is_linked(trees, monkeypatch):
    _, _, shared, _ = trees
    monkeypatch.setenv("AGENTIHOOKS_SHARED_BUNDLE_PATH", str(shared))

    assert install._get_shared_bundle_path() == shared.resolve()
    assert install._under_managed_source(str(shared / ".claude" / "skills" / "team"))


def test_a_linked_shared_bundle_beats_the_environment(trees, monkeypatch):
    _, _, shared, user = trees
    monkeypatch.setenv("AGENTIHOOKS_SHARED_BUNDLE_PATH", str(user))
    bundles.link(shared, shared=True)

    assert install._get_shared_bundle_path() == shared


def test_profile_names_resolve_root_then_shared_then_user_and_never_the_other_way(trees):
    builtin, root, shared, user = trees
    roles = profile_chain.PACKAGE_ROLES
    for folder in (
        root / "profiles" / "a",
        shared / "profiles" / "a",
        user / "profiles" / "a",
        shared / "profiles" / "b",
        user / "profiles" / "b",
        user / "profiles" / "c",
    ):
        folder.mkdir(parents=True)

    assert profile_chain.profile_candidates(root, "a", {}, user, shared=shared) == [
        ("a", [builtin / "a", root / "profiles" / "a", shared / "profiles" / "a", user / "profiles" / "a", roles / "a"])
    ]
    assert profile_chain.profile_dirs(root, "a,b,c", {}, user, shared=shared) == [
        ("a", root / "profiles" / "a"),
        ("b", shared / "profiles" / "b"),
        ("c", user / "profiles" / "c"),
    ]
    assert profile_chain.rendered_dirs(root, "a,b", {}, user, shared=shared) == [
        ("a", root / "profiles" / "a"),
        ("b", shared / "profiles" / "b"),
    ]
    assert profile_chain.profile_dirs(root, "b", {}, user) == [("b", user / "profiles" / "b")]


def test_profile_collisions_name_every_pair_and_the_two_slot_call_is_unchanged(trees, capsys):
    _, root, shared, user = trees
    for folder in (
        root / "profiles" / "x",
        shared / "profiles" / "x",
        shared / "profiles" / "y",
        user / "profiles" / "y",
        user / "profiles" / "z",
        root / "profiles" / "z",
    ):
        folder.mkdir(parents=True)
    _link_all(root, shared, user)

    assert profile_chain.profile_collisions(root, user) == ["z"]
    assert profile_chain.profile_collisions(root, user, shared=shared) == ["x", "y", "z"]
    bundles.warn_unavailable()
    out = capsys.readouterr().out
    assert "Profile 'x' exists in the root and the shared bundle - the root's is used." in out
    assert "Profile 'y' exists in the shared and the user bundle - the shared bundle's is used." in out
    assert "Profile 'z' exists in the root and the user bundle - the root's is used." in out


def test_force_init_keeps_the_shared_bundle_with_the_other_two(trees, monkeypatch):
    _, root, shared, user = trees
    _link_all(root, shared, user)
    before = _state()
    monkeypatch.setattr(install, "_clean_state_dir", lambda: install.STATE_JSON.unlink())

    _init(monkeypatch, force=True)

    after = _state()
    for key in ("bundle", "shared_bundle", "user_bundle"):
        assert after[key]["path"] == before[key]["path"]


def test_status_names_the_shared_bundle_only_when_one_is_linked(trees, monkeypatch):
    from unittest.mock import patch

    from scripts import status_checker

    _, root, shared, _ = trees
    monkeypatch.setattr(status_checker, "STATE_JSON", install.STATE_JSON)
    bundles.link(root)

    def report():
        with patch("hooks._redis.get_redis", return_value=None):
            results = status_checker.run_all_checks()
        return results["profile"], status_checker.format_cli(results)

    plain, text = report()
    assert plain["shared_bundle"] == "(none)"
    assert "shared bundle" not in text

    bundles.link(shared, shared=True)
    linked, text = report()
    assert linked["shared_bundle"] == str(shared)
    assert linked["shared_bundle_ok"] is True
    assert f"shared bundle: {shared}" in text

    shared.rename(shared.with_name("moved"))
    gone, text = report()
    assert gone["shared_bundle_ok"] is False
    assert f"[!!] shared bundle: {shared}" in text


def test_init_prints_the_shared_bundle_between_the_root_and_the_user_bundle(tmp_path, capsys):
    from unittest.mock import Mock

    from scripts.init_reporter import report_target

    adapter = Mock()
    adapter.name = "codex"
    adapter.home.return_value = tmp_path
    adapter.summary_counts.return_value = {"mcp": 0, "skills": 0, "plugins": 0}
    record = {
        "profile": "a",
        "profile_chain": ["a"],
        "bundle": tmp_path / "root",
        "shared_bundle": tmp_path / "shared",
        "user_bundle": tmp_path / "user",
    }

    report_target(lambda: None, adapter, lambda: record)

    lines = capsys.readouterr().out.splitlines()
    assert lines.index(f"Bundle {tmp_path / 'root'}") + 1 == lines.index(f"Shared bundle {tmp_path / 'shared'}")
    assert lines.index(f"Shared bundle {tmp_path / 'shared'}") + 1 == lines.index(f"User bundle {tmp_path / 'user'}")
