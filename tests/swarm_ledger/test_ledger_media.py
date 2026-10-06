import ledger_core as core
import pytest

from scripts.swarm_ledger import ledger_media


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
