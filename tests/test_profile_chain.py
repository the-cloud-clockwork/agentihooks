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
    roles = profile_chain.PACKAGE_ROLES
    assert profile_chain.profile_candidates(bundle, "child", links) == [
        ("parent", [builtin / "parent", bundle / "profiles" / "parent", linked, roles / "parent"]),
        ("external", [builtin / "external", bundle / "profiles" / "external", linked, roles / "external"]),
        ("child", [builtin / "child", child, roles / "child"]),
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
    monkeypatch.setattr("install.PROFILES_DIR", profiles_root)
    monkeypatch.setattr(install, "_get_bundle_path", lambda: None)
    monkeypatch.setattr("install._get_bundle_path", lambda: None)
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


@pytest.fixture
def package_roles(tmp_path, monkeypatch):
    roles = tmp_path / "package-roles"
    roles.mkdir()
    monkeypatch.setattr(profile_chain, "PACKAGE_ROLES", roles)

    def create(name, parents=None):
        root = roles / name
        root.mkdir()
        if parents is not None:
            (root / "profile.yml").write_text(yaml.safe_dump({"extends": parents}))
        return root

    return create


def test_package_role_resolves_when_no_other_root_defines_it(profiles, package_roles):
    _create, resolve = profiles
    role = package_roles("engineer")
    assert resolve("engineer") == [("engineer", role)]


def test_bundle_profile_wins_over_package_role(profiles, package_roles):
    create, resolve = profiles
    package_roles("engineer")
    bundle_role = create("engineer")
    assert resolve("engineer") == [("engineer", bundle_role)]


def test_bundle_profile_extending_package_role_layers_on_top(profiles, package_roles):
    create, resolve = profiles
    base = package_roles("base")
    package_role = package_roles("engineer", ["base"])
    bundle_role = create("engineer", ["package:engineer"])
    assert resolve("engineer") == [("base", base), ("package:engineer", package_role), ("engineer", bundle_role)]


def test_package_prefix_reaches_only_the_package_roles_folder(tmp_path, package_roles):
    bundle = tmp_path / "bundle"
    (bundle / "profiles" / "engineer").mkdir(parents=True)
    role = package_roles("engineer")
    assert profile_chain.profile_candidates(bundle, "package:engineer", {}) == [("package:engineer", [role])]
    assert profile_chain.profile_candidates(bundle, "engineer", {"engineer": tmp_path})[0][1][-2:] == [tmp_path, role]


def test_install_labels_and_lists_package_roles(tmp_path, monkeypatch, package_roles):
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    monkeypatch.setattr(install, "PROFILES_DIR", builtin)
    install.STATE_JSON.write_text("{}")
    role = package_roles("engineer")
    assert install._resolve_profile_dir("package:engineer") == role
    assert install._profile_source_label("engineer") == "package"
    assert install._profile_source_label("package:engineer") == "package"
    package_roles("_draft")
    (role.parent / "notes.md").write_text("not a role\n")
    assert install._available_profiles() == ["engineer"]
    assert install._resolve_profile_dir("package:missing") is None


def test_enforcements_and_conditions_load_from_package_role(tmp_path, monkeypatch, package_roles):
    from hooks.context import conditions, enforcement

    role = package_roles("engineer")
    entry = {"id": "pkg-1", "message": "package rule", "cadence": 3}
    (role / "enforcements.json").write_text(json.dumps({"enforcements": [entry]}))
    monkeypatch.setattr(profile_chain, "BUILT_IN_PROFILES", tmp_path / "builtin")
    monkeypatch.setattr(enforcement, "_get_bundle_path", lambda: None)
    monkeypatch.setattr(enforcement, "_get_active_profile", lambda: "engineer")
    monkeypatch.setattr(enforcement, "_get_linked_profiles", lambda: {})
    assert [e["id"] for e in enforcement._load_profile_enforcements()] == ["pkg-1"]
    monkeypatch.setenv("AGENTIHOOKS_PROFILE", "engineer")
    layers, _probed = conditions.layer_dirs({})
    assert ("profile:engineer", role / ".claude" / "conditions") in layers


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [(None, []), ("", []), ("name: x\n", []), ("name: x\nallowedOverlays: [router, brain]\n", ["router", "brain"])],
)
def test_overlays_reads_the_declared_overlays(tmp_path, manifest, expected):
    if manifest is not None:
        (tmp_path / "profile.yml").write_text(manifest)

    assert profile_chain.overlays(tmp_path) == expected


@pytest.mark.parametrize("wrapped", [False, True])
def test_rendered_overlays_read_the_home_stamp(tmp_path, wrapped):
    stamp = tmp_path / profile_chain.RENDER_STAMP
    assert profile_chain.rendered_overlays(tmp_path) == []
    data = {"chain": ["a", "brain"], "overlays": ["brain"]}
    stamp.write_text(json.dumps({"render": data, "operator": "digest"} if wrapped else data))
    assert profile_chain.rendered_overlays(tmp_path) == ["brain"]
    for data in (
        {"chain": ["a", "brain"]},
        {"overlays": None},
        [],
        None,
        {"render": {}},
        {"render": None},
        {"render": []},
    ):
        stamp.write_text(json.dumps(data))
        assert profile_chain.rendered_overlays(tmp_path) == []
    stamp.write_text("{")
    assert profile_chain.rendered_overlays(tmp_path) == []


def test_rendered_dirs_adds_the_overlays_the_chain_declares(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_chain, "BUILT_IN_PROFILES", tmp_path / "builtin")
    monkeypatch.setattr(profile_chain, "PACKAGE_ROLES", tmp_path / "roles")
    bundle = tmp_path / "bundle"
    base = bundle / "profiles" / "base"
    role = bundle / "profiles" / "role"
    solo = bundle / "profiles" / "solo"
    brain = tmp_path / "linked" / "brain"
    for path in (base, role, solo, brain):
        path.mkdir(parents=True)
    (base / "profile.yml").write_text("allowedOverlays: [router, brain]\n")
    (role / "profile.yml").write_text("extends: [base]\n")
    linked = {"brain": brain}

    assert profile_chain.rendered_dirs(bundle, "role", linked) == [("base", base), ("role", role), ("brain", brain)]
    assert profile_chain.rendered_dirs(bundle, "solo", linked) == [("solo", solo)]
    assert profile_chain.rendered_dirs(bundle, "role", {}) == [("base", base), ("role", role)]


@pytest.fixture
def overlay_profiles(tmp_path, monkeypatch):
    roles = tmp_path / "roles"
    monkeypatch.setattr(profile_chain, "PACKAGE_ROLES", roles)
    found = {}
    for name, text in {
        "a": "kind: overlay\nwears: [engineer]\n",
        "b": "kind: overlay\nwears: [engineer, qa]\n",
        "c": "kind: overlay\nwears: [engineer]\n",
        "d": "kind: overlay\nwears: [engineer]\n",
        "planning": "kind: overlay\nwears: [planner]\n",
        "unkinded": "wears: [engineer]\n",
        "plain": "name: plain\n",
    }.items():
        found[name] = tmp_path / "bundle" / name
        found[name].mkdir(parents=True)
        (found[name] / "profile.yml").write_text(text)
    (roles / "engineer").mkdir(parents=True)
    chain = [("base", tmp_path / "bundle" / "base"), ("package:engineer", roles / "engineer"), ("eng", tmp_path / "e")]
    return chain, found.get


def test_wears_reads_only_an_overlay_manifest(overlay_profiles, tmp_path):
    _, resolve = overlay_profiles
    assert profile_chain.wears(resolve("b")) == ["engineer", "qa"]
    assert profile_chain.wears(resolve("unkinded")) == []
    assert profile_chain.wears(resolve("plain")) == []
    assert profile_chain.wears(tmp_path / "absent") == []
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "profile.yml").write_text("kind: overlay\nwears:\n")
    assert profile_chain.wears(empty) == []


@pytest.mark.parametrize("roles", ["engineer", "5", "{engineer: true}"])
def test_wears_refuses_a_value_that_is_not_a_list(tmp_path, roles):
    overlay = tmp_path / "tuner"
    overlay.mkdir()
    (overlay / "profile.yml").write_text(f"kind: overlay\nwears: {roles}\n")
    with pytest.raises(ValueError) as refused:
        profile_chain.wears(overlay)
    assert str(refused.value) == "overlay tuner wears must be a list of roles"
    (overlay / "profile.yml").write_text(f"wears: {roles}\n")
    assert profile_chain.wears(overlay) == []


def test_role_is_the_package_base_role_of_the_chain(overlay_profiles, tmp_path):
    chain, _ = overlay_profiles
    assert profile_chain.role(chain) == "engineer"
    assert profile_chain.role([("eng", tmp_path / "e")]) is None
    assert profile_chain.role([]) is None


def test_worn_lists_each_chosen_overlay_once_in_sorted_order(overlay_profiles):
    chain, resolve = overlay_profiles
    assert profile_chain.worn(chain, ["c", "a", "c", "b"], resolve) == ["a", "b", "c"]
    assert profile_chain.worn(chain, [], resolve) == []
    assert profile_chain.worn([], [], resolve) == []


def test_worn_refuses_more_than_three_overlays(overlay_profiles):
    chain, resolve = overlay_profiles
    with pytest.raises(ValueError) as refused:
        profile_chain.worn(chain, ["a", "b", "c", "d"], resolve)
    assert str(refused.value) == "an agent wears at most 3 overlays; 4 were chosen: a, b, c, d"


@pytest.mark.parametrize(
    "chosen,message",
    [
        (["a", "planning"], "overlay planning does not wear the engineer role"),
        (["unkinded"], "overlay unkinded does not wear the engineer role"),
        (["plain"], "overlay plain does not wear the engineer role"),
        (["gone"], "overlay gone not found"),
    ],
)
def test_worn_refuses_an_overlay_that_does_not_wear_the_role(overlay_profiles, chosen, message):
    chain, resolve = overlay_profiles
    with pytest.raises(ValueError) as refused:
        profile_chain.worn(chain, chosen, resolve)
    assert str(refused.value) == message


def test_worn_refuses_any_overlay_on_a_chain_without_a_base_role(overlay_profiles, tmp_path):
    _, resolve = overlay_profiles
    with pytest.raises(ValueError) as refused:
        profile_chain.worn([("eng", tmp_path / "e")], ["a"], resolve)
    assert str(refused.value) == "overlay a needs a chain with a package base role"
