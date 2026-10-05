"""Trace locators: every trace row names the store or file its directive came from."""

import json
import subprocess
from unittest.mock import patch

import pytest

from hooks.context import injection_trace
from scripts.trace_cli import main as trace

SID = "sess-locator-1"


@pytest.fixture()
def stores(tmp_path):
    bundle = tmp_path / "bundle"
    profile = bundle / "profiles" / "anton"
    profile.mkdir(parents=True)
    (bundle / "enforcements.json").write_text(
        json.dumps({"enforcements": [{"id": "b-1", "message": "b", "cadence": 3}]})
    )
    (profile / "enforcements.json").write_text(
        json.dumps({"enforcements": [{"id": "p-1", "message": "p", "cadence": 3}]})
    )
    runtime = tmp_path / "runtime" / "enforcements.json"
    runtime.parent.mkdir()
    runtime.write_text(json.dumps({"enforcements": [{"id": "r-1", "message": "r", "cadence": 3}]}))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    with (
        patch("hooks.context.enforcement._store_path", return_value=runtime),
        patch("hooks.context.enforcement._counter_path", return_value=tmp_path / "counters.json"),
        patch("hooks.context.enforcement._delivery_path", return_value=tmp_path / "delivery.json"),
        patch("hooks.context.enforcement._get_bundle_path", return_value=bundle),
        patch("hooks.context.enforcement._get_active_profile", return_value="anton"),
    ):
        from hooks.context import enforcement

        local = enforcement._local_store_path(repo, create_parent=True)
        local.write_text(json.dumps({"enforcements": [{"id": "l-1", "message": "l", "cadence": 3}]}))
        yield {"bundle": bundle, "profile": profile, "runtime": runtime, "local": local, "repo": repo}


@pytest.fixture()
def broadcast_file(tmp_path):
    path = tmp_path / "broadcast.json"
    path.write_text("[]")
    with (
        patch("hooks.context.broadcast._broadcast_path", return_value=path),
        patch("hooks.context.broadcast.BASE_CHANNELS", ["brain"]),
    ):
        yield path


def _locators():
    return {row["source"]: row.get("locator") for row in injection_trace.trace(SID)}


def test_each_enforcement_names_the_store_it_was_loaded_from(stores):
    from hooks.context import enforcement

    assert enforcement.get_session_start_enforcements(SID, cwd=stores["repo"])

    assert _locators() == {
        "b-1": {"store": str(stores["bundle"] / "enforcements.json"), "id": "b-1"},
        "p-1": {"store": str(stores["profile"] / "enforcements.json"), "id": "p-1"},
        "r-1": {"store": str(stores["runtime"]), "id": "r-1"},
        "l-1": {"store": str(stores["local"]), "id": "l-1"},
    }


def test_a_condition_names_its_file_and_layer():
    from hooks.context import conditions

    entry = {"file": "pre-bash.guard.sh", "path": "/profiles/anton/conditions/pre-bash.guard.sh", "source": "profile"}
    conditions.merge("pre", {"session_id": SID, "tool_name": "Bash"}, [(entry, {"returncode": 0, "stdout": "mind"})])

    assert _locators() == {
        "pre-bash.guard.sh": {"layer": "profile", "file": "/profiles/anton/conditions/pre-bash.guard.sh"}
    }


def test_a_broadcast_names_its_message_id(broadcast_file):
    from hooks.context.broadcast import create_broadcast, get_broadcast_context

    message_id = create_broadcast("Deploy freeze", "alert", source="operator")
    assert get_broadcast_context(SID, [message_id])

    assert _locators() == {message_id: {"id": message_id}}


def test_a_brain_entry_names_its_entry_id_and_file(broadcast_file, tmp_path):
    from hooks.context import brain_adapter
    from hooks.context.broadcast import get_broadcast_context

    brain_dir = tmp_path / "feed"
    brain_dir.mkdir()
    marker = brain_dir / "hot-arcs.md"
    marker.write_text("---\nid: arc-42\ntitle: Hot Arcs\n---\nArc forty two is hot.\n")
    brain_adapter._publish_entries(brain_adapter.FileBrainSource(brain_dir).fetch())
    message_id = json.loads(broadcast_file.read_text())[0]["id"]
    assert get_broadcast_context(SID, [message_id])

    assert _locators() == {message_id: {"id": "arc-42", "file": str(marker)}}


def test_a_republished_brain_entry_keeps_the_new_file(broadcast_file, tmp_path):
    from hooks.context import brain_adapter

    entry = brain_adapter.BrainEntry(id="arc-1", title="T", content="body", file="/old/arc.md")
    brain_adapter._publish_entries([entry])
    moved = brain_adapter.BrainEntry(id="arc-1", title="T", content="body", file="/new/arc.md")
    brain_adapter._publish_entries([moved])

    assert [m["origin"] for m in json.loads(broadcast_file.read_text())] == [{"id": "arc-1", "file": "/new/arc.md"}]


def test_a_correction_copies_the_locator_and_both_listings_print_it(stores, capsys):
    from hooks.context import enforcement

    enforcement.get_session_start_enforcements(SID, cwd=stores["repo"])
    assert trace([SID, "--wrong", "p-1", "--repo", "/repos/qitp", "--reason", "not here"]) == 0
    capsys.readouterr()

    assert injection_trace.corrections()[-1]["locator"] == {
        "store": str(stores["profile"] / "enforcements.json"),
        "id": "p-1",
    }
    want = f"store={stores['profile'] / 'enforcements.json'} id=p-1"
    assert trace(["--corrections"]) == 0
    assert want in capsys.readouterr().out
    assert trace([SID]) == 0
    out = capsys.readouterr().out
    assert any(line.split("\t")[2] == "p-1" and line.split("\t")[3] == want for line in out.splitlines())
