import argparse
import json
import sys
from pathlib import Path

import pytest
import yaml

from hooks.context import profile_chain
from scripts import install


@pytest.fixture(autouse=True)
def installer_home(_isolate_real_user_paths, monkeypatch):
    import install as legacy_install

    for name in ("CLAUDE_HOME", "AGENTIHOOKS_STATE_DIR", "STATE_JSON", "_CLAUDE_JSON", "_BASHRC", "AGENTIHOOKS_ROOT"):
        monkeypatch.setattr(install, name, getattr(legacy_install, name))


@pytest.fixture(params=["runtime", "install"])
def profiles(tmp_path, monkeypatch, request):
    builtin = tmp_path / "builtin"
    bundle = tmp_path / "bundle"
    builtin.mkdir()
    (bundle / "profiles").mkdir(parents=True)
    monkeypatch.setattr(profile_chain, "BUILT_IN_PROFILES", builtin)
    monkeypatch.setattr(install, "PROFILES_DIR", builtin)
    state = {"bundle": {"path": str(bundle)}}
    install.STATE_JSON.write_text(json.dumps(state))

    def create(name, parents=None):
        root = bundle / "profiles" / name
        root.mkdir()
        if parents is not None:
            (root / "profile.yml").write_text(yaml.safe_dump({"extends": parents}))
        return root

    def resolve(chain):
        if request.param == "runtime":
            return profile_chain.profile_dirs(bundle, chain, {})
        return install._resolve_profile_chain(chain)

    return create, resolve


def test_parents_resolve_depth_first_left_to_right_before_child(profiles):
    create, resolve = profiles
    base = create("base")
    left = create("left", ["base"])
    right = create("right")
    child = create("child", ["left", "right"])
    assert resolve("child") == [("base", base), ("left", left), ("right", right), ("child", child)]


def test_diamond_and_explicit_chain_resolve_each_name_once(profiles):
    create, resolve = profiles
    base = create("base")
    left = create("left", ["base"])
    right = create("right", ["base"])
    child = create("child", ["left", "right"])
    assert resolve(" child, right, base, child, ,") == [
        ("base", base),
        ("left", left),
        ("right", right),
        ("child", child),
    ]


@pytest.mark.parametrize("parents, message", [(["child"], "child -> child"), (["left"], "child -> left -> child")])
def test_cycle_error_names_the_chain(profiles, parents, message):
    create, resolve = profiles
    create("child", parents)
    create("left", ["child"])
    with pytest.raises(ValueError) as error:
        resolve("child")
    assert str(error.value) == f"Profile inheritance cycle: {message}"


def test_unknown_parent_error_names_the_referring_chain(profiles):
    create, resolve = profiles
    create("child", ["left"])
    create("left", ["missing"])
    with pytest.raises(ValueError) as error:
        resolve("child")
    assert str(error.value) == "Profile 'missing' not found in inheritance chain: child -> left -> missing"


def test_separate_comma_chain_members_expand_in_order(profiles):
    create, resolve = profiles
    base = create("base")
    first = create("first", ["base"])
    second = create("second", ["base"])
    assert resolve("first,second") == [("base", base), ("first", first), ("second", second)]


def test_empty_chain_and_empty_manifest(profiles):
    create, resolve = profiles
    empty = create("empty")
    (empty / "profile.yml").write_text("")
    assert resolve(" , ") == []
    assert resolve("empty") == [("empty", empty)]


def test_absent_profile_has_no_candidates():
    assert profile_chain.profile_candidates(None, None, {}) == []
    assert profile_chain.profile_candidates(None, "", {}) == []


def test_unavailable_explicit_profile_keeps_the_available_chain(profiles):
    create, resolve = profiles
    existing = create("existing")
    assert resolve("missing,existing") == [("existing", existing)]


def test_parent_lookup_keeps_builtin_bundle_and_linked_precedence(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    bundle = tmp_path / "bundle"
    linked = tmp_path / "linked"
    for root in (builtin / "parent", bundle / "profiles" / "parent", linked):
        root.mkdir(parents=True)
    child = bundle / "profiles" / "child"
    child.mkdir()
    (child / "profile.yml").write_text("extends: [parent, external]\n")
    monkeypatch.setattr(profile_chain, "BUILT_IN_PROFILES", builtin)
    monkeypatch.setattr(install, "PROFILES_DIR", builtin)
    install.STATE_JSON.write_text(
        json.dumps(
            {
                "bundle": {"path": str(bundle)},
                "linked_profiles": [{"name": name, "path": str(linked)} for name in ("parent", "external")],
            }
        )
    )
    links = {"parent": linked, "external": linked}
    expected = [("parent", builtin / "parent"), ("external", linked), ("child", child)]
    assert profile_chain.profile_dirs(bundle, "child", links) == expected
    assert install._resolve_profile_chain("child") == expected
    assert profile_chain.profile_candidates(bundle, "child", links) == [
        ("parent", [builtin / "parent", bundle / "profiles" / "parent", linked]),
        ("external", [builtin / "external", bundle / "profiles" / "external", linked]),
        ("child", [builtin / "child", child]),
    ]


def test_init_installs_inherited_assets_and_child_settings(tmp_path, monkeypatch):
    profiles_root = tmp_path / "profiles"
    for name, parents in (("base", []), ("child", ["base"])):
        root = profiles_root / name
        (root / ".claude").mkdir(parents=True)
        (root / "profile.yml").write_text(yaml.safe_dump({"extends": parents}))
        (root / "CLAUDE.md").write_text(f"{name} persona\n")
        (root / ".claude" / "settings.overrides.json").write_text(
            json.dumps({"env": {"WINNER": name, name.upper(): "present"}})
        )
    parent = profiles_root / "base" / ".claude"
    for kind in ("rules", "agents"):
        (parent / kind).mkdir()
        (parent / kind / "inherited.md").write_text("inherited asset\n")
    (parent / "skills" / "inherited").mkdir(parents=True)
    (parent / "skills" / "inherited" / "SKILL.md").write_text("inherited skill\n")
    (parent / ".mcp.json").write_text(json.dumps({"mcpServers": {"inherited": {"command": "echo"}}}))
    base = profiles_root / "_base"
    base.mkdir()
    (base / "settings.base.json").write_text('{"hooks": {}}')
    monkeypatch.setattr(install, "PROFILES_DIR", profiles_root)
    monkeypatch.setattr(install, "_get_bundle_path", lambda: None)
    monkeypatch.setattr(install, "_install_cli_tool", lambda: None)
    monkeypatch.setattr(install, "_seed_user_env_file", lambda: [])
    monkeypatch.setattr(install, "_resolve_hooks_python", lambda: Path(sys.executable))
    monkeypatch.setattr("install._resolve_hooks_python", lambda: Path(sys.executable))
    install.install_global(argparse.Namespace(profile="child", settings_profile="", install_target="claude"))
    home = Path.home()
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["env"]["WINNER"] == "child"
    assert settings["env"]["BASE"] == "present"
    assert (home / ".claude" / "rules" / "inherited.md").read_text() == "inherited asset\n"
    assert (home / ".claude" / "agents" / "inherited.md").read_text() == "inherited asset\n"
    assert (home / ".claude" / "skills" / "inherited" / "SKILL.md").read_text() == "inherited skill\n"
    assert json.loads((home / ".claude.json").read_text())["mcpServers"]["inherited"]["command"] == "echo"
    persona = (home / ".claude" / "CLAUDE.md").read_text()
    assert persona.index("base persona") < persona.index("child persona")
    assert install._global_record(install._load_state())["profile"] == "child"


@pytest.mark.parametrize(
    "parents, message",
    [
        (["child"], "Profile inheritance cycle: child -> child"),
        (["missing"], "Profile 'missing' not found in inheritance chain: child -> missing"),
    ],
)
def test_init_reports_invalid_inheritance_before_writing(tmp_path, monkeypatch, capsys, parents, message):
    profiles_root = tmp_path / "profiles"
    child = profiles_root / "child"
    child.mkdir(parents=True)
    (child / "profile.yml").write_text(yaml.safe_dump({"extends": parents}))
    monkeypatch.setattr(install, "PROFILES_DIR", profiles_root)
    monkeypatch.setattr(install, "_get_bundle_path", lambda: None)
    with pytest.raises(SystemExit) as error:
        install.install_global(argparse.Namespace(profile="child", settings_profile="", install_target="claude"))
    assert error.value.code == 1
    assert capsys.readouterr().err == f"ERROR: {message}\n"
    assert not (Path.home() / ".claude" / "settings.json").exists()


@pytest.mark.parametrize(
    "parents, identity, layers",
    [
        ({"child": ["base", "kit"]}, "child", "Layered on top: **brain** (a capability layer"),
        ({}, "base", "Layered on top: **kit**, **child**, **brain** (capability layers"),
    ],
)
def test_persona_identity_skips_profiles_reached_through_extends(tmp_path, monkeypatch, parents, identity, layers):
    from scripts.targets import _common

    dirs = []
    for name in ("base", "kit", "child", "brain"):
        directory = tmp_path / name
        directory.mkdir()
        if name in parents:
            (directory / "profile.yml").write_text(yaml.safe_dump({"extends": parents[name]}))
        dirs.append((name, directory))
    monkeypatch.setattr(_common, "linked_profile_names", lambda: {"brain"})
    text = _common.build_persona(dirs, [name for name, _ in dirs], None, [], "HEAD", "FOOT")
    assert f"You are **{identity}**" in text
    assert f"answer as **{identity}**" in text
    assert layers in text
