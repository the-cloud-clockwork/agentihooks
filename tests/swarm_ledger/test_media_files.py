import shutil

import ledger_core as core
import pytest

from scripts.swarm_ledger import ledger_artifacts, ledger_bin, ledger_media
from scripts.swarm_ledger.repository import bin_storage, media_files
from tests.swarm_ledger.test_bin import make_ledger
from tests.swarm_ledger.test_media import png


def test_an_image_another_server_stored_is_served_without_its_disk_copy():
    make_ledger("shared-media")
    entry = ledger_media.store("shared-media", png(4, 5))
    shutil.rmtree(ledger_media.folder("shared-media"))
    assert ledger_media.served("shared-media", entry["id"]) == png(4, 5)
    assert ledger_media.entry("shared-media", entry["id"]) == entry


def test_an_entry_naming_an_image_another_server_stored_is_written():
    make_ledger("shared-entry")
    entry = ledger_media.store("shared-entry", png(6, 2))
    shutil.rmtree(ledger_media.folder("shared-entry"))
    op = {"op": "add", "thread": "chat", "id": "m-shared", "text": "", "attachments": [{"id": entry["id"]}]}
    state, rejected = core.sync("shared-entry", ops=ledger_media.resolve("shared-entry", [op]))
    assert rejected == []
    assert state["chat"][-1]["attachments"] == [entry]


def test_serving_a_disk_only_image_adopts_it_into_the_database():
    data = png(3, 7)
    media_id = ledger_media.store("adopted-media", data)["id"]
    media_files.drop("adopted-media", {media_id})
    assert ledger_media.read("adopted-media", media_id) == data
    assert media_files.load("adopted-media", media_id) is None
    assert ledger_media.served("adopted-media", media_id) == data
    shutil.rmtree(ledger_media.folder("adopted-media"))
    assert ledger_media.served("adopted-media", media_id) == data


def test_a_malformed_media_id_is_refused():
    with pytest.raises(ValueError, match="not a media id"):
        ledger_media.read("kept", "../a.png")


def test_a_missing_image_is_refused():
    with pytest.raises(ValueError, match="no stored image"):
        ledger_media.read("kept", f"{'0' * 64}.png")


def test_attachment_paths_name_only_images_on_this_disk():
    make_ledger("path-media")
    here, there = ledger_media.store("path-media", png(1, 2)), ledger_media.store("path-media", png(2, 1))
    (ledger_media.folder("path-media") / there["id"]).unlink()
    op = {"op": "add", "thread": "chat", "id": "m-paths", "text": "", "attachments": [here, there]}
    state, rejected = core.sync("path-media", ops=[op])
    assert rejected == []
    events = [{"target": "chat", "id": "m-paths"}, {"target": "chat", "id": "m-none"}]
    doc = {key: value for key, value in state.items() if key != "_meta"}
    ledger_media.attach_paths("path-media", doc, events)
    assert events == [
        {
            "target": "chat",
            "id": "m-paths",
            "image_paths": [str((ledger_media.folder("path-media") / here["id"]).resolve())],
        },
        {"target": "chat", "id": "m-none"},
    ]


def test_an_entry_whose_images_are_all_elsewhere_names_no_paths():
    make_ledger("away-media")
    entry = ledger_media.store("away-media", png(5, 1))
    shutil.rmtree(ledger_media.folder("away-media"))
    state, _ = core.sync(
        "away-media", ops=[{"op": "add", "thread": "chat", "id": "m-away", "text": "", "attachments": [entry]}]
    )
    events = [{"target": "chat", "id": "m-away"}]
    ledger_media.attach_paths("away-media", {key: value for key, value in state.items() if key != "_meta"}, events)
    assert events == [{"target": "chat", "id": "m-away"}]


def test_attachment_paths_skip_an_id_that_is_not_a_media_id():
    outside = ledger_media.folder("escape-media").parent / "outside.png"
    outside.write_bytes(png(1, 1))
    doc = {"chat": [{"id": "m-escape", "attachments": [{"id": "../outside.png"}]}]}
    events = [{"target": "chat", "id": "m-escape"}]
    ledger_media.attach_paths("escape-media", doc, events)
    assert events == [{"target": "chat", "id": "m-escape"}]


def test_a_sweep_drops_old_unused_image_rows_after_the_write_commits():
    make_ledger("sweep-rows")
    files = [ledger_media.store("sweep-rows", png(n, n)) for n in range(1, 4)]
    media_files.save("sweep-rows", files[0]["id"], png(1, 1), 1)
    media_files.save("sweep-rows", files[1]["id"], png(2, 2), 1)
    shutil.rmtree(ledger_media.folder("sweep-rows"))
    op = {"op": "add", "thread": "chat", "id": "m-kept", "text": "", "attachments": [files[0]]}
    state, rejected = core.sync("sweep-rows", ops=[op])
    assert rejected == []
    found = [media_files.load("sweep-rows", entry["id"]) for entry in files]
    assert found == [png(1, 1), None, png(3, 3)]


def test_media_sweep_returns_the_expired_unused_rows_at_the_cutoff():
    media_files.save("cutoff-media", "a.png", b"a", 1_000)
    media_files.save("cutoff-media", "b.png", b"b", 1_001)
    media_files.save("cutoff-media", "c.png", b"c", 0)
    media_files.save("other-media", "d.png", b"d", 0)
    assert ledger_media.sweep("cutoff-media", {"c.png"}, 3_601_000) == set()
    assert ledger_media.sweep("cutoff-media", {"c.png"}, 3_601_001) == {"a.png"}


def test_a_reupload_renews_the_row_grace():
    media_files.save("renew-media", "a.png", b"a", 1)
    media_files.save("renew-media", "a.png", b"a", 5_000_000)
    assert ledger_media.sweep("renew-media", set(), 5_000_001) == set()


def test_storing_an_image_saves_its_upload_time(monkeypatch):
    monkeypatch.setattr(core, "now_ms", lambda: 7_000_000)
    media_id = ledger_media.store("timed-media", png(8, 8))["id"]
    assert media_files.ages("timed-media") == {media_id: 7_000_000}


def test_only_the_named_images_of_one_ledger_are_dropped():
    media_files.save("drop-media", "a.png", b"one", 1)
    media_files.save("drop-media", "b.png", b"two", 1)
    media_files.save("other-drop", "a.png", b"three", 1)
    media_files.drop("drop-media", {"a.png", "missing.png"})
    media_files.drop("drop-media", set())
    found = [media_files.load(slug, name) for slug, name in (("drop-media", "a.png"), ("drop-media", "b.png"))]
    assert found == [None, b"two"]
    assert media_files.load("other-drop", "a.png") == b"three"


def test_an_artifact_sweep_drops_unused_rows_of_both_stores():
    make_ledger("both-stores")
    state = core.sync("both-stores")[0]
    media_files.save("both-stores", "old.png", b"x", 0)
    ctx = core.Context(state["_meta"], 3_600_001)
    ledger_artifacts.sweep("both-stores", {key: value for key, value in state.items() if key != "_meta"}, ctx)
    assert ctx.unstored == {"old.png"}


def test_a_ledger_purged_from_the_bin_takes_its_images_with_it():
    media_files.save("binned-media", "a.png", b"gone", 1)
    media_files.save("kept-media", "a.png", b"stays", 1)
    ledger_bin.delete("binned-media", now=0)
    assert "binned-media" in bin_storage.purge_expired(now=ledger_bin.KEEP_DAYS * ledger_bin.DAY_MS + 1)
    assert media_files.load("binned-media", "a.png") is None
    assert media_files.load("kept-media", "a.png") == b"stays"
