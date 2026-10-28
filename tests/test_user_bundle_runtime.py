import json
from pathlib import Path

import pytest

from scripts import install
from scripts.profiles import bundles
from tests.test_user_bundle_install import _link_both, _write, world  # noqa: F401


def _bundle_state(**linked: Path) -> None:
    state = {key: {"path": str(path)} for key, path in linked.items()}
    state["targets"] = {"global": {"claude": {"profile": "alpha"}}}
    install.STATE_JSON.write_text(json.dumps(state))


@pytest.fixture
def layers_world(world, monkeypatch):  # noqa: F811
    from hooks.context import profile_chain

    monkeypatch.setattr(profile_chain, "state_path", lambda: install.STATE_JSON)
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    for bundle in (world["root"], world["user"]):
        (bundle / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
    return world


def test_condition_layers_run_package_root_user_profile_runtime_in_order(layers_world):
    from hooks.context import conditions, profile_chain

    _bundle_state(bundle=layers_world["root"], user_bundle=layers_world["user"])

    layers, _probed = conditions.layer_dirs(profile_chain.read_state())

    assert [source for source, _ in layers] == ["package", "bundle", "user-bundle", "profile:alpha", "runtime"]
    assert dict(layers)["user-bundle"] == layers_world["user"] / ".claude" / "conditions"


def test_a_user_condition_overrides_the_root_one_of_the_same_key_and_a_profile_overrides_both(layers_world):
    from hooks.context import conditions, profile_chain

    for bundle, text in ((layers_world["root"], "root"), (layers_world["user"], "user")):
        _write(bundle / ".claude" / "conditions" / "pre-bash.git-guard.sh", f"#!/bin/sh\necho {text}\n")
    _write(layers_world["user"] / ".claude" / "conditions" / "pre-bash.only-user.sh", "#!/bin/sh\n")
    _bundle_state(bundle=layers_world["root"], user_bundle=layers_world["user"])

    entries, _invalid = conditions.scan_layers(conditions.layer_dirs(profile_chain.read_state())[0])
    by_file = {entry["file"]: entry["source"] for entry in entries}

    assert by_file["pre-bash.git-guard.sh"] == "user-bundle"
    assert by_file["pre-bash.only-user.sh"] == "user-bundle"

    profile_layer = layers_world["root"] / "profiles" / "alpha" / ".claude" / "conditions"
    _write(profile_layer / "pre-bash.git-guard.sh", "#!/bin/sh\n")
    entries, _invalid = conditions.scan_layers(conditions.layer_dirs(profile_chain.read_state())[0])
    assert {entry["file"]: entry["source"] for entry in entries}["pre-bash.git-guard.sh"] == "profile:alpha"


def test_a_machine_without_a_user_bundle_keeps_its_condition_layers(layers_world):
    from hooks.context import conditions, profile_chain

    _bundle_state(bundle=layers_world["root"])

    layers, _probed = conditions.layer_dirs(profile_chain.read_state())

    assert [source for source, _ in layers] == ["package", "bundle", "profile:alpha", "runtime"]


def test_the_user_scope_is_a_destination_and_never_the_permission_to_create(layers_world):
    from hooks.context import conditions

    _bundle_state(bundle=layers_world["root"], user_bundle=layers_world["user"])
    args = {"step": "pre", "matcher": "bash", "name": "guard", "script": "exit 0", "session_id": "sid-p84"}

    with pytest.raises(conditions.ConditionError, match="operator's own prompt"):
        conditions.create_condition(**args, scope="user")
    assert not (layers_world["user"] / ".claude" / "conditions").exists()

    conditions.arm_gate("sid-p84")
    made = conditions.create_condition(**args, scope="user")

    assert made["layer"] == "user-bundle"
    assert Path(made["path"]) == layers_world["user"] / ".claude" / "conditions" / "pre-bash-guard.sh"
    assert not (layers_world["root"] / ".claude" / "conditions").exists()


def test_the_global_scope_still_writes_to_the_root_bundle_when_a_user_bundle_is_linked(layers_world):
    from hooks.context import conditions

    _bundle_state(bundle=layers_world["root"], user_bundle=layers_world["user"])
    conditions.arm_gate("sid-p84b")

    made = conditions.create_condition(
        step="pre", matcher="bash", name="guard", script="exit 0", session_id="sid-p84b", scope="global"
    )

    assert made["layer"] == "bundle"
    assert not (layers_world["user"] / ".claude" / "conditions").exists()


def test_the_user_scope_without_a_user_bundle_names_the_missing_link(layers_world):
    from hooks.context import conditions

    _bundle_state(bundle=layers_world["root"])
    conditions.arm_gate("sid-p84c")

    with pytest.raises(conditions.ConditionError, match="needs a linked user bundle"):
        conditions.create_condition(
            step="pre", matcher="bash", name="guard", script="exit 0", session_id="sid-p84c", scope="user"
        )


def test_removing_a_condition_by_user_scope_leaves_the_root_file(layers_world):
    from hooks.context import conditions

    name = "pre-bash-guard.sh"
    for bundle in (layers_world["root"], layers_world["user"]):
        _write(bundle / ".claude" / "conditions" / name, "#!/bin/sh\n")
    _bundle_state(bundle=layers_world["root"], user_bundle=layers_world["user"])
    conditions.arm_gate("sid-p84d")

    removed = conditions.remove_condition(file=name, session_id="sid-p84d", scope="user")

    assert removed["layer"] == "user-bundle"
    assert not (layers_world["user"] / ".claude" / "conditions" / name).exists()
    assert (layers_world["root"] / ".claude" / "conditions" / name).is_file()


def _store(*entries: dict) -> str:
    return json.dumps({"enforcements": list(entries)})


def _entry(identifier: str, message: str) -> dict:
    return {"id": identifier, "message": message, "cadence": 5}


def test_enforcements_merge_root_then_user_then_profile_by_id(layers_world, monkeypatch):
    from hooks.context import enforcement

    _write(layers_world["root"] / "enforcements.json", _store(_entry("a", "root a"), _entry("b", "root b")))
    _write(layers_world["user"] / "enforcements.json", _store(_entry("b", "user b"), _entry("c", "user c")))
    _write(layers_world["user"] / "profiles" / "alpha" / "enforcements.json", _store(_entry("c", "profile c")))
    _bundle_state(bundle=layers_world["root"], user_bundle=layers_world["user"])
    monkeypatch.setattr(enforcement, "_load_store", lambda: [])
    monkeypatch.setattr(enforcement, "_load_local_enforcements", lambda cwd: [])

    merged = {e["id"]: (e["message"], e["source"]) for e in enforcement.load_all_enforcements()}

    assert merged == {"a": ("root a", "bundle"), "b": ("user b", "user_bundle"), "c": ("profile c", "profile")}


def test_a_root_only_machine_loads_its_enforcements_unchanged(layers_world, monkeypatch):
    from hooks.context import enforcement

    _write(layers_world["root"] / "enforcements.json", _store(_entry("a", "m")))
    _bundle_state(bundle=layers_world["root"])
    monkeypatch.setattr(enforcement, "_load_store", lambda: [])
    monkeypatch.setattr(enforcement, "_load_local_enforcements", lambda cwd: [])

    assert [(e["id"], e["source"]) for e in enforcement.load_all_enforcements()] == [("a", "bundle")]


def test_lifecycle_roots_merge_by_id_root_then_user(layers_world):
    from hooks.lifecycle import config

    def root(identifier: str, path: str) -> dict:
        return {"id": identifier, "kind": "scratch", "path": path}

    _write(layers_world["root"] / "lifecycle.json", json.dumps({"roots": [root("a", "/r/a"), root("b", "/r/b")]}))
    _write(layers_world["user"] / "lifecycle.json", json.dumps({"roots": [root("b", "/u/b"), root("c", "/u/c")]}))
    _bundle_state(bundle=layers_world["root"], user_bundle=layers_world["user"])

    paths = {r.id: r.path for r in config.load_roots()}

    assert paths["a"] == "/r/a"
    assert paths["b"] == "/u/b"
    assert paths["c"] == "/u/c"


def test_a_user_lifecycle_root_can_be_disabled_without_touching_the_root_entry(layers_world):
    from hooks.lifecycle import config

    entry = {"id": "a", "kind": "scratch", "path": "/r/a"}
    _write(layers_world["root"] / "lifecycle.json", json.dumps({"roots": [entry]}))
    _write(layers_world["user"] / "lifecycle.json", json.dumps({"roots": [{"id": "a", "enabled": False}]}))
    _bundle_state(bundle=layers_world["root"], user_bundle=layers_world["user"])

    assert "a" not in {r.id for r in config.load_roots()}
    assert json.loads((layers_world["root"] / "lifecycle.json").read_text())["roots"] == [entry]


def test_workflow_layers_put_the_user_bundle_between_the_bundle_and_the_profiles(layers_world):
    from hooks.context import profile_chain
    from scripts.workflows import loader

    _bundle_state(bundle=layers_world["root"], user_bundle=layers_world["user"])

    layers = loader.layer_dirs(profile_chain.read_state(), None)

    assert [name for name, _ in layers] == ["package", "bundle", "user-bundle", "profile:alpha"]


def test_the_launch_check_counts_a_user_bundle_profile_as_bundled(layers_world):
    from scripts.swarm import launch_check

    (layers_world["user"] / "profiles" / "mine").mkdir()
    _bundle_state(bundle=layers_world["root"], user_bundle=layers_world["user"])

    assert launch_check.bundled("mine") is True
    assert launch_check.bundled("alpha") is True
    assert launch_check.bundled("nowhere") is False


def _overlay(bundle: Path, name: str, wears: str = "engineer") -> None:
    _write(bundle / "profiles" / name / "profile.yml", f"kind: overlay\nwears: [{wears}]\n")


def test_the_overlay_inventory_lists_both_bundles_and_the_root_wins_a_shared_name(world):  # noqa: F811
    from scripts.swarm import overlays

    _overlay(world["root"], "tuner")
    _overlay(world["root"], "shared", "engineer")
    _overlay(world["user"], "mine", "qa")
    _overlay(world["user"], "shared", "qa")
    _link_both(world)

    assert overlays.available() == [
        {"name": "mine", "wears": ["qa"]},
        {"name": "shared", "wears": ["engineer"]},
        {"name": "tuner", "wears": ["engineer"]},
    ]


def test_a_root_only_machine_offers_exactly_its_root_overlays(world):  # noqa: F811
    from scripts.swarm import overlays

    _overlay(world["root"], "tuner")
    bundles.link(world["root"])

    assert overlays.available() == [{"name": "tuner", "wears": ["engineer"]}]


@pytest.fixture
def scaffolding(world, monkeypatch):  # noqa: F811
    from scripts.profiles import scaffold

    monkeypatch.setattr("hooks.context.profile_chain.PACKAGE_ROLES", world["root"].parent / "roles")
    (world["root"].parent / "roles" / "engineer").mkdir(parents=True)
    return scaffold


def test_overlay_new_with_a_user_bundle_demands_an_explicit_destination(scaffolding, world, capsys):  # noqa: F811
    _link_both(world)

    assert scaffolding.main(["overlay", "new", "tuner", "--wears", "engineer"]) == 1

    assert "pass --dest root or --dest user" in capsys.readouterr().err
    assert not (world["root"] / "profiles" / "tuner").exists()
    assert not (world["user"] / "profiles" / "tuner").exists()


def test_overlay_new_writes_only_into_the_chosen_bundle(scaffolding, world):  # noqa: F811
    _link_both(world)

    assert scaffolding.main(["overlay", "new", "mine", "--wears", "engineer", "--dest", "user"]) == 0
    assert scaffolding.main(["overlay", "new", "shared", "--wears", "engineer", "--dest", "root"]) == 0

    assert (world["user"] / "profiles" / "mine" / "profile.yml").is_file()
    assert not (world["root"] / "profiles" / "mine").exists()
    assert (world["root"] / "profiles" / "shared" / "profile.yml").is_file()
    assert not (world["user"] / "profiles" / "shared").exists()
    assert json.loads(install.STATE_JSON.read_text())["bundle"]["path"] == str(world["root"])


def test_overlay_new_in_a_user_destination_needs_a_linked_user_bundle(scaffolding, world, capsys):  # noqa: F811
    bundles.link(world["root"])

    assert scaffolding.main(["overlay", "new", "mine", "--wears", "engineer", "--dest", "user"]) == 1

    assert "no user bundle linked" in capsys.readouterr().err


def test_overlay_new_without_a_user_bundle_needs_no_destination(scaffolding, world):  # noqa: F811
    bundles.link(world["root"])

    assert scaffolding.main(["overlay", "new", "tuner", "--wears", "engineer"]) == 0
    assert (world["root"] / "profiles" / "tuner" / "profile.yml").is_file()


def test_overlay_check_reads_the_named_destination(scaffolding, world, capsys):  # noqa: F811
    _link_both(world)
    scaffolding.main(["overlay", "new", "mine", "--wears", "engineer", "--dest", "user"])
    capsys.readouterr()

    assert scaffolding.main(["overlay", "check", "mine", "--dest", "user"]) == 0
    assert scaffolding.main(["overlay", "check", "mine"]) == 1
    assert "not found in the linked bundle" in capsys.readouterr().err


def test_bundle_new_user_links_beside_the_root_without_replacing_it(scaffolding, world, tmp_path):  # noqa: F811
    bundles.link(world["root"])
    fresh = tmp_path / "fresh-user"

    assert scaffolding.main(["bundle", "new", "--user", str(fresh)]) == 0

    state = json.loads(install.STATE_JSON.read_text())
    assert state["bundle"]["path"] == str(world["root"])
    assert state["user_bundle"]["path"] == str(fresh.resolve())
    assert (fresh / "profiles").is_dir()
    assert not (world["root"] / "README.md").exists()
