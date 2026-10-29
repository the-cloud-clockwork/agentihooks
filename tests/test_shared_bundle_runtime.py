import json
from pathlib import Path

import pytest

from scripts.profiles import bundles
from tests.test_user_bundle_install import _write, world  # noqa: F401
from tests.test_user_bundle_runtime import _bundle_state, _entry, _overlay, _store  # noqa: F401


@pytest.fixture
def layers3(world, monkeypatch):  # noqa: F811
    from hooks.context import profile_chain
    from scripts import install

    monkeypatch.setattr(profile_chain, "state_path", lambda: install.STATE_JSON)
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    shared = world["root"].parent / "shared"
    _write(shared / "profiles" / ".keep", "")
    found = {**world, "shared": shared}
    for name in ("root", "shared", "user"):
        (found[name] / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
    return found


def _state_all(layers3) -> None:
    _bundle_state(bundle=layers3["root"], shared_bundle=layers3["shared"], user_bundle=layers3["user"])


def test_condition_layers_run_package_root_shared_user_profile_runtime_in_order(layers3):
    from hooks.context import conditions, profile_chain

    _state_all(layers3)

    layers, _probed = conditions.layer_dirs(profile_chain.read_state())

    assert [source for source, _ in layers] == [
        "package",
        "bundle",
        "shared-bundle",
        "user-bundle",
        "profile:alpha",
        "runtime",
    ]
    assert dict(layers)["shared-bundle"] == layers3["shared"] / ".claude" / "conditions"


def test_a_later_bundle_condition_overrides_an_earlier_one_of_the_same_key(layers3):
    from hooks.context import conditions, profile_chain

    for bundle in ("root", "shared", "user"):
        _write(layers3[bundle] / ".claude" / "conditions" / "pre-bash.clash.sh", f"#!/bin/sh\necho {bundle}\n")
    _write(layers3["root"] / ".claude" / "conditions" / "pre-bash.rs.sh", "#!/bin/sh\necho root\n")
    _write(layers3["shared"] / ".claude" / "conditions" / "pre-bash.rs.sh", "#!/bin/sh\necho shared\n")
    _write(layers3["shared"] / ".claude" / "conditions" / "pre-bash.only-shared.sh", "#!/bin/sh\n")
    _state_all(layers3)

    entries, _invalid = conditions.scan_layers(conditions.layer_dirs(profile_chain.read_state())[0])
    by_file = {entry["file"]: entry["source"] for entry in entries}

    assert by_file["pre-bash.clash.sh"] == "user-bundle"
    assert by_file["pre-bash.rs.sh"] == "shared-bundle"
    assert by_file["pre-bash.only-shared.sh"] == "shared-bundle"


def test_a_machine_without_a_shared_bundle_keeps_its_condition_layers(layers3):
    from hooks.context import conditions, profile_chain

    _bundle_state(bundle=layers3["root"], user_bundle=layers3["user"])

    layers, _probed = conditions.layer_dirs(profile_chain.read_state())

    assert [source for source, _ in layers] == ["package", "bundle", "user-bundle", "profile:alpha", "runtime"]


def test_the_shared_scope_is_a_destination_for_a_linked_shared_bundle_only(layers3):
    from hooks.context import conditions

    _bundle_state(bundle=layers3["root"], user_bundle=layers3["user"])
    with pytest.raises(conditions.ConditionError, match="scope 'shared' needs a linked shared bundle"):
        conditions.target_dir("shared")

    _state_all(layers3)
    assert conditions.target_dir("shared") == ("shared-bundle", layers3["shared"] / ".claude" / "conditions")
    assert conditions.target_dir("global")[0] == "bundle"


def test_enforcements_merge_root_then_shared_then_user_then_profile_by_id(layers3, monkeypatch):
    from hooks.context import enforcement

    _write(layers3["root"] / "enforcements.json", _store(_entry("a", "root a"), _entry("b", "root b")))
    _write(
        layers3["shared"] / "enforcements.json",
        _store(_entry("b", "shared b"), _entry("c", "shared c"), _entry("d", "shared d")),
    )
    _write(layers3["user"] / "enforcements.json", _store(_entry("c", "user c")))
    _write(layers3["root"] / "profiles" / "alpha" / "enforcements.json", _store(_entry("d", "profile d")))
    _state_all(layers3)
    monkeypatch.setattr(enforcement, "_load_store", lambda: [])
    monkeypatch.setattr(enforcement, "_load_local_enforcements", lambda cwd: [])

    merged = {e["id"]: (e["message"], e["source"]) for e in enforcement.load_all_enforcements()}

    assert merged == {
        "a": ("root a", "bundle"),
        "b": ("shared b", "shared_bundle"),
        "c": ("user c", "user_bundle"),
        "d": ("profile d", "profile"),
    }


def test_the_injection_trace_files_a_shared_enforcement_under_the_bundle_layer():
    from hooks.context import injection_trace

    assert injection_trace._ENFORCEMENT_LAYER["shared_bundle"] == "bundle"


def test_the_trace_sweep_looks_in_the_shared_bundle_stores_and_profiles(layers3, monkeypatch, tmp_path):
    from hooks.context import trace_sweep

    _state_all(layers3)
    monkeypatch.setattr(trace_sweep, "_repo_dirs", lambda root: [])

    stores, condition_dirs = trace_sweep._stores(tmp_path)

    assert layers3["shared"] / "enforcements.json" in stores
    assert layers3["shared"] / ".claude" / "conditions" in condition_dirs
    assert layers3["shared"] / "profiles" / "alpha" in trace_sweep._profile_dirs()


def test_lifecycle_roots_merge_by_id_root_then_shared_then_user(layers3):
    from hooks.lifecycle import config

    def root(identifier: str, path: str) -> dict:
        return {"id": identifier, "kind": "scratch", "path": path}

    _write(layers3["root"] / "lifecycle.json", json.dumps({"roots": [root("a", "/r/a"), root("b", "/r/b")]}))
    _write(
        layers3["shared"] / "lifecycle.json",
        json.dumps({"roots": [root("b", "/s/b"), root("c", "/s/c"), root("d", "/s/d")]}),
    )
    _write(layers3["user"] / "lifecycle.json", json.dumps({"roots": [root("c", "/u/c")]}))
    _state_all(layers3)

    paths = {r.id: r.path for r in config.load_roots()}

    assert paths == {"a": "/r/a", "b": "/s/b", "c": "/u/c", "d": "/s/d"}


def test_a_user_lifecycle_entry_can_disable_a_shared_root(layers3):
    from hooks.lifecycle import config

    entry = {"id": "a", "kind": "scratch", "path": "/s/a"}
    _write(layers3["shared"] / "lifecycle.json", json.dumps({"roots": [entry]}))
    _write(layers3["user"] / "lifecycle.json", json.dumps({"roots": [{"id": "a", "enabled": False}]}))
    _state_all(layers3)

    assert "a" not in {r.id for r in config.load_roots()}
    assert json.loads((layers3["shared"] / "lifecycle.json").read_text())["roots"] == [entry]


def test_workflow_layers_put_the_shared_bundle_between_the_bundle_and_the_user_bundle(layers3):
    from hooks.context import profile_chain
    from scripts.workflows import loader

    _state_all(layers3)

    layers = loader.layer_dirs(profile_chain.read_state(), None)

    assert [name for name, _ in layers] == ["package", "bundle", "shared-bundle", "user-bundle", "profile:alpha"]


def test_the_launch_check_counts_a_shared_bundle_profile_as_bundled(layers3):
    from scripts.swarm import launch_check

    (layers3["shared"] / "profiles" / "team").mkdir()
    _state_all(layers3)

    assert launch_check.bundled("team") is True
    assert launch_check.bundled("nowhere") is False


def test_the_overlay_inventory_lists_all_three_bundles_and_the_earlier_wins_a_name(layers3):
    from scripts.swarm import overlays

    _overlay(layers3["root"], "tuner")
    _overlay(layers3["root"], "dup", "engineer")
    _overlay(layers3["shared"], "team", "designer")
    _overlay(layers3["shared"], "dup", "designer")
    _overlay(layers3["user"], "mine", "qa")
    _overlay(layers3["user"], "team", "qa")
    bundles.link(layers3["root"])
    bundles.link(layers3["user"], user=True)
    bundles.link(layers3["shared"], shared=True)

    assert overlays.available() == [
        {"name": "dup", "wears": ["engineer"]},
        {"name": "mine", "wears": ["qa"]},
        {"name": "team", "wears": ["designer"]},
        {"name": "tuner", "wears": ["engineer"]},
    ]


def test_the_overlay_revision_has_three_parts_whenever_a_shared_bundle_is_linked(layers3):
    from scripts.swarm import overlays

    commits = {name: _commit(layers3[name]) for name in ("root", "shared", "user")}
    bundles.link(layers3["root"])
    assert overlays.revision() == commits["root"]

    bundles.link(layers3["user"], user=True)
    assert overlays.revision() == f"{commits['root']}:{commits['user']}"

    bundles.link(layers3["shared"], shared=True)
    assert overlays.revision() == f"{commits['root']}:{commits['shared']}:{commits['user']}"

    bundles.unlink(user=True)
    assert overlays.revision() == f"{commits['root']}:{commits['shared']}:"


def test_a_shared_bundle_without_a_commit_is_encoded_as_a_dash_in_the_revision_with_one_warning(layers3, capsys):
    from scripts.swarm import overlays, runtime

    commits = {name: _commit(layers3[name]) for name in ("root", "user")}
    bundles.link(layers3["root"])
    bundles.link(layers3["user"], user=True)
    bundles.link(layers3["shared"], shared=True)

    revision = overlays.revision()

    assert revision == f"{commits['root']}:-:{commits['user']}"
    err = capsys.readouterr().err
    assert err.count("WARNING") == 1
    assert f"the shared bundle at {layers3['shared']} has no commit to record; launching without it" in err
    assert runtime._pinned(["tuner"], revision) == ["--bundle-revision", revision]

    bundles.unlink(user=True)
    assert overlays.revision() == f"{commits['root']}:-:"


def _commit(root: Path) -> str:
    import os
    import subprocess

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", *args], cwd=root, env=env, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
