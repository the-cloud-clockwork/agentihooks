import hashlib
import shutil

import pytest

from scripts.swarm_ledger import ledger_artifacts, ledger_bin, plan_ranges
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger.repository import artifact_files, bin_storage, media_files
from tests.swarm_ledger import test_plan_ranges
from tests.swarm_ledger.test_media import png
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


def test_a_malformed_artifact_id_is_refused(published):
    with pytest.raises(ValueError, match="not an artifact id"):
        ledger_artifacts.read(published, "../plan.md")


def _disk_only(slug, name, data):
    folder = ledger_artifacts.media.folder(slug)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)


def test_the_start_pass_adopts_a_disk_only_plan_into_the_database():
    data = test_plan_ranges.PLAN.encode()
    file_id = f"{hashlib.sha256(data).hexdigest()}.md"
    _disk_only("start-plan", file_id, data)
    ledger_artifacts.adopt_disk("start-plan")
    assert artifact_files.load("start-plan", file_id) == data
    assert media_files.ages("start-plan") == {}
    shutil.rmtree(ledger_artifacts.media.folder("start-plan"))
    assert ledger_artifacts.read("start-plan", file_id) == data


def test_the_start_pass_adopts_a_disk_only_image_into_both_stores(monkeypatch):
    data = png(3, 4)
    media_id = f"{hashlib.sha256(data).hexdigest()}.png"
    _disk_only("start-image", media_id, data)
    monkeypatch.setattr(ledger_artifacts.core, "now_ms", lambda: 7_000_000)
    ledger_artifacts.adopt_disk("start-image")
    assert artifact_files.load("start-image", media_id) == data
    assert media_files.ages("start-image") == {media_id: 7_000_000}


def test_the_start_pass_keeps_an_image_the_database_already_holds(monkeypatch):
    data = png(5, 2)
    media_id = f"{hashlib.sha256(data).hexdigest()}.png"
    _disk_only("start-kept", media_id, data)
    media_files.save("start-kept", media_id, data, 5)
    monkeypatch.setattr(ledger_artifacts.core, "now_ms", lambda: 7_000_000)
    ledger_artifacts.adopt_disk("start-kept")
    assert media_files.ages("start-kept") == {media_id: 5}


def test_the_start_pass_skips_a_name_that_is_not_a_stored_id():
    _disk_only("start-skip", "notes.md", b"# notes\n")
    ledger_artifacts.adopt_disk("start-skip")
    assert artifact_files.load("start-skip", "notes.md") is None
    assert media_files.ages("start-skip") == {}


def test_the_start_pass_of_a_ledger_without_a_folder_stores_nothing():
    ledger_artifacts.adopt_disk("start-none")
    assert media_files.ages("start-none") == {}
