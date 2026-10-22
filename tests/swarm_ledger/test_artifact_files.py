import hashlib
import shutil

import pytest

from scripts.swarm_ledger import ledger_artifacts, ledger_bin, plan_ranges
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger.repository import artifact_files, bin_storage
from tests.swarm_ledger import test_plan_ranges
from tests.swarm_ledger.test_plan_kind import add
from tests.swarm_ledger.test_plan_publish import task

plan_ledger = test_plan_ranges.plan_ledger
published = test_plan_ranges.published


def test_a_task_add_reads_a_published_plan_another_server_stored(published):
    shutil.rmtree(ledger_artifacts.media.folder(published))
    state, rejected = add(published, "first", plan_slice="first")
    assert (rejected, state["_meta"]["warnings"]) == ([], [])
    assert task(state, "first")["plan_lines"] == "4-6"


def test_a_stored_artifact_is_served_without_its_disk_copy(published):
    state = core.sync(published)[0]
    [artifact] = state["artifacts"]
    file_id = artifact["file"]["id"]
    shutil.rmtree(ledger_artifacts.media.folder(published))
    assert plan_ranges.stored_text(state["phases"][0]["plan_ref"], state) == test_plan_ranges.PLAN
    assert ledger_artifacts.read(published, file_id) == test_plan_ranges.PLAN.encode()
    assert ledger_artifacts.entry(published, file_id) == artifact["file"]


def test_serving_a_disk_only_artifact_adopts_it_into_the_database():
    data = test_plan_ranges.PLAN.encode()
    file_id = f"{hashlib.sha256(data).hexdigest()}.md"
    folder = ledger_artifacts.media.folder("adopted")
    folder.mkdir(parents=True)
    (folder / file_id).write_bytes(data)
    assert ledger_artifacts.read("adopted", file_id) == data
    assert artifact_files.load("adopted", file_id) is None
    assert ledger_artifacts.served("adopted", file_id) == data
    assert artifact_files.load("adopted", file_id) == data
    shutil.rmtree(folder)
    assert ledger_artifacts.served("adopted", file_id) == data


def test_a_purged_artifact_leaves_the_database(published):
    state = core.sync(published)[0]
    file_id = state["artifacts"][0]["file"]["id"]
    assert artifact_files.load(published, file_id) == test_plan_ranges.PLAN.encode()
    state, rejected = core.sync(published, ops=[{"op": "artifact_purge", "id": "purge", "by": "planner"}])
    assert (rejected, state["artifacts"]) == ([], [])
    assert artifact_files.load(published, file_id) is None
    with pytest.raises(ValueError, match="no stored artifact"):
        ledger_artifacts.read(published, file_id)


def test_only_the_named_artifacts_of_one_ledger_are_dropped(plan_ledger):
    artifact_files.save(plan_ledger, "a.md", b"one")
    artifact_files.save(plan_ledger, "b.md", b"two")
    artifact_files.save("other", "a.md", b"three")
    artifact_files.save(plan_ledger, "a.md", b"ignored")
    artifact_files.drop(plan_ledger, {"a.md", "missing.md"})
    found = [artifact_files.load(slug, name) for slug, name in ((plan_ledger, "a.md"), (plan_ledger, "b.md"))]
    assert found == [None, b"two"]
    assert artifact_files.load("other", "a.md") == b"three"


def test_a_ledger_purged_from_the_bin_takes_its_artifacts_with_it():
    artifact_files.save("binned", "a.md", b"gone")
    artifact_files.save("kept", "a.md", b"stays")
    ledger_bin.delete("binned", now=0)
    assert "binned" in bin_storage.purge_expired(now=ledger_bin.KEEP_DAYS * ledger_bin.DAY_MS + 1)
    assert artifact_files.load("binned", "a.md") is None
    assert artifact_files.load("kept", "a.md") == b"stays"


def test_a_malformed_artifact_id_is_refused_before_any_lookup(published):
    with pytest.raises(ValueError, match="not an artifact id"):
        ledger_artifacts.read(published, "../plan.md")
