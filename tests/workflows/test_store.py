import json

import pytest

from hooks import config
from scripts.workflows import machine, schema, store


def tiny() -> schema.Workflow:
    return schema.validate(
        {
            "workflow": {"name": "tiny", "intent": "Build it.", "head": "build", "exit": "build.pass"},
            "stages": {"build": {"profile": "engineer", "next": {"pass": "exit"}}},
        }
    )


def test_create_writes_the_machine_state_plus_the_run_fields(tmp_path):
    folder = tmp_path / "nested" / "run-1"
    definition = tiny()

    state = store.create(folder, definition, "fix the thing")

    assert state == {
        **machine.start(definition),
        "workflow": "tiny",
        "run": "run-1",
        "case": "fix the thing",
    }
    assert store.load(folder) == state
    assert (folder / "outputs").is_dir()


def test_the_state_file_is_named_workflow_json(tmp_path):
    folder = tmp_path / "run-1"
    state = store.create(folder, tiny(), "case")

    assert json.loads((folder / "workflow.json").read_text()) == state


def test_create_refuses_a_folder_that_already_exists(tmp_path):
    folder = tmp_path / "run-1"
    folder.mkdir()

    with pytest.raises(FileExistsError):
        store.create(folder, tiny(), "case")


def test_save_overwrites_the_existing_state_leaving_no_temp_file(tmp_path):
    folder = tmp_path / "run-1"
    first = store.create(folder, tiny(), "case")
    changed = {**first, "visit": 9}

    store.save(folder, changed)

    assert store.load(folder) == changed
    assert sorted(entry.name for entry in folder.iterdir()) == ["outputs", "workflow.json"]


def test_a_failed_replace_leaves_the_previous_state_intact(tmp_path, monkeypatch):
    folder = tmp_path / "run-1"
    first = store.create(folder, tiny(), "case")

    def boom(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(store.os, "replace", boom)

    with pytest.raises(OSError, match="disk full"):
        store.save(folder, {**first, "visit": 9})

    assert store.load(folder) == first


def test_default_root_honours_agentihooks_home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", tmp_path)

    assert store.default_root() == tmp_path / "workflows" / "runs"
