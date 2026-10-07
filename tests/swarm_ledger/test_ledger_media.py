import os

import ledger_artifacts as artifacts
import ledger_core as core
import pytest

from scripts.swarm_ledger import ledger_media
from tests.swarm_ledger.test_bin import make_ledger
from tests.swarm_ledger.test_media import png


def plant(slug):
    folder = core.LEDGER_DIR / f"{slug}.media"
    folder.mkdir(exist_ok=True)
    (folder / "a.png").write_bytes(b"x")
    return folder / "a.png"


@pytest.mark.parametrize("slug", ["", "..", "kept/..", "../ledger", "/abs", None])
def test_purge_refuses_a_name_that_is_not_a_ledger_slug(slug):
    kept = plant("kept")
    (core.LEDGER_DIR / ".media").mkdir(exist_ok=True)
    with pytest.raises(ValueError, match="refusing to purge media for .*: not a ledger slug"):
        ledger_media.purge(slug)
    assert kept.exists()
    assert (core.LEDGER_DIR / ".media").is_dir()


def test_purge_removes_only_the_named_ledgers_media():
    gone, kept = plant("gone"), plant("kept")
    ledger_media.purge("gone")
    ledger_media.purge("gone")
    assert not gone.parent.exists()
    assert kept.exists()


def test_cleanup_removes_old_orphans_and_keeps_references_and_pending_uploads():
    make_ledger("sweep-media")
    files = [ledger_media.store("sweep-media", png(n, n)) for n in range(1, 6)]
    paths = [ledger_media.folder("sweep-media") / entry["id"] for entry in files]
    for path in paths[:-1]:
        os.utime(path, (1, 1))
    state, rejected = core.sync(
        "sweep-media",
        ops=[{"op": "add", "thread": "chat", "id": "m-image", "text": "", "attachments": [files[0]]}],
    )
    assert not rejected
    assert paths[0].exists()
    assert not paths[1].exists()
    assert paths[4].exists()
    doc = {k: v for k, v in state.items() if k != "_meta"}
    paths[2].write_bytes(png(3, 3))
    paths[3].write_bytes(png(4, 4))
    os.utime(paths[2], (1, 1))
    os.utime(paths[3], (1, 1))
    doc["artifacts"] = [{"file": files[2]}]
    doc["artifact_trash"] = [{"file": files[3], "deleted_at": core.now_ms()}]
    artifacts.sweep("sweep-media", doc, core.Context(state["_meta"], core.now_ms()))
    assert paths[2].exists()
    assert paths[3].exists()


def test_pending_upload_expires_after_one_hour_and_nonfiles_are_kept():
    make_ledger("pending-media")
    file = ledger_media.store("pending-media", png(9, 9))
    path = ledger_media.folder("pending-media") / file["id"]
    directory = path.parent / "directory"
    directory.mkdir()
    doc = core.sync("pending-media")[0]
    os.utime(path, (10_000, 10_000))
    os.utime(directory, (1, 1))
    ctx = core.Context(doc["_meta"], 13_600_000)
    artifacts.sweep("pending-media", doc, ctx)
    assert path.exists()
    ctx.at += 1
    artifacts.sweep("pending-media", doc, ctx)
    assert not path.exists()
    assert directory.is_dir()


@pytest.mark.parametrize("artifact", [False, True])
def test_reupload_renews_the_pending_entry_grace(artifact):
    make_ledger("reupload-media")
    data = png(7, 7)

    def upload():
        return (
            artifacts.store("reupload-media", "image.png", data)
            if artifact
            else ledger_media.store("reupload-media", data)
        )

    entry = upload()
    path = ledger_media.folder("reupload-media") / entry["id"]
    os.utime(path, (1, 1))
    assert upload() == entry
    core.sync("reupload-media")
    assert path.read_bytes() == data
