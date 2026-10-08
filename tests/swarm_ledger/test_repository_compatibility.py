import io
import json

import ledger_core as legacy_core
import pytest

from scripts.swarm_ledger import ledger, ledger_bin, ledger_core, ledger_server, new_ledger
from scripts.swarm_ledger.repository import bin_storage
from scripts.swarm_ledger.repository.file import FileLedgerRepository


@pytest.fixture
def stored(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    monkeypatch.setattr(legacy_core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(ledger_core, "LEDGER_DIR", tmp_path)
    repo = FileLedgerRepository()
    content = {"title": "Stored", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    repo.create("compatibility", content)
    return repo


def test_legacy_load_state_retains_its_document_and_metadata(stored):
    expected = stored.get_document("compatibility")
    doc, meta, created = ledger_core.load_state(ledger_core.paths("compatibility")[1], None)
    assert {**doc, "_meta": meta} == expected and created is False


def test_bin_facades_keep_timestamps_and_retention(stored):
    ledger_bin.bin_closed("compatibility", closed_at=1, now=2)
    assert ledger_bin.entries() == {"compatibility": 2}
    ledger_bin._save({"compatibility": 100})
    assert ledger_bin.entries() == {"compatibility": 100}
    assert ledger_bin.restore("compatibility", now=101)
    assert ledger_bin.restored()["compatibility"] == 101
    assert ledger_bin.bin_closed("compatibility", closed_at=100, now=102) is False
    assert ledger_bin.entries() == {}
    ledger_bin.delete("compatibility", now=200)
    assert ledger_bin.entries()["compatibility"] == 200
    assert ledger_bin.auto_bin(now=201) == []
    assert ledger_bin.purge_expired(now=200 + 31 * ledger_bin.DAY_MS) == ["compatibility"]
    assert not stored.exists("compatibility")


@pytest.mark.parametrize("token", ["demo-ledger-token", ""])
@pytest.mark.parametrize("upload", [False, True])
def test_command_token_headers_follow_the_stored_page(stored, monkeypatch, tmp_path, token, upload):
    page = stored.read_page("compatibility")
    existing = legacy_core.read_token(page)
    stored.write_page("compatibility", page.replace(existing, token))
    requests = []

    def receive(request, timeout):
        requests.append(request)
        return io.BytesIO(b'{"ok": true}' if upload else b'{"data": {"ok": true}}')

    monkeypatch.setattr(ledger.urllib.request, "urlopen", receive)
    if upload:
        source = tmp_path / "image.png"
        source.write_bytes(b"image")
        assert ledger.upload("compatibility", "eng", str(source), "media", {}) == {"ok": True}
    else:
        assert ledger.resource("compatibility", "metadata") == {"ok": True}
    assert requests[0].get_header("X-ledger-token") == token


def test_server_existence_requires_both_slug_and_storage(stored):
    handler = ledger_server.Handler.__new__(ledger_server.Handler)
    assert handler.exists("compatibility")
    assert not handler.exists("missing-compatible")
    assert not handler.exists("../compatibility")


def test_page_read_and_upload_membership_use_the_repository(stored):
    slug = "compatibility"
    stored_page = stored.read_page(slug)
    page = ledger_server.page_for(slug)
    assert f'<meta name="ledger-token" content="{legacy_core.read_token(stored_page)}">' in page
    assert not legacy_core.SEED_RE.search(page)
    assert stored.read_page(slug) == stored_page
    stored.apply_ops(slug, ops=[{"op": "join", "id": "join", "by": "eng"}])
    stored.write_page(slug, stored.read_page(slug).replace('"overview": "o"', '"overview": "agent edit"'))
    handler = ledger_server.Handler.__new__(ledger_server.Handler)
    handler.headers = {
        "Host": sorted(ledger_server.ALLOWED_HOSTS)[0],
        "X-Ledger-Token": legacy_core.read_token(stored.read_page(slug)),
        "X-Ledger-Agent": "eng",
        "Content-Length": "4",
    }
    handler.rfile = io.BytesIO(b"data")
    replies, types = [], []

    def send(code, body, ctype):
        replies.append((code, body))
        types.append(ctype)

    handler.send = send
    ledger_server.Handler.receive(handler, slug, 100, lambda data: {"size": len(data)})
    assert replies == [(200, json.dumps({"size": 4}))]
    assert stored.read_snapshot(slug)["overview"] == "o"
    handler.headers["X-Ledger-Agent"] = "unjoined"
    ledger_server.Handler.receive(handler, slug, 100, lambda data: {})
    assert replies[-1] == (403, "agent must join this ledger before uploading")
    assert types[-1] == "text/plain"
    state = stored.read_snapshot(slug)
    del state["_meta"]["members"]
    legacy_core.paths(slug)[1].write_text(json.dumps(state))
    ledger_server.Handler.receive(handler, slug, 100, lambda data: {})
    assert replies[-1] == (403, "agent must join this ledger before uploading")


def test_seed_watcher_reconciles_a_changed_page(stored, monkeypatch):
    slug = "compatibility"
    stored.write_page(slug, stored.read_page(slug).replace('"overview": "o"', '"overview": "edited"'))

    def finish(interval):
        raise RuntimeError("watch ended")

    monkeypatch.setattr(ledger_server.time, "sleep", finish)
    with pytest.raises(RuntimeError, match="watch ended"):
        ledger_server.watch_seeds()
    assert stored.get_document(slug, reconcile=False)["overview"] == "edited"


def test_creator_joins_the_small_ledger_with_its_original_event(stored):
    from tests.swarm_ledger.test_small_ledger import create, state

    assert new_ledger.create("compatibility", {}) is False
    create("joined-compatibility", "--as", "worker")
    doc = state("joined-compatibility")
    assert doc["_meta"]["members"]["worker"]["role"] == "member"
    [event] = [e for e in doc["_meta"]["events"] if e["kind"] == "joined"]
    assert event["by"] == "worker"
    assert bin_storage.entries() == ledger_bin.entries()


def test_the_served_shell_reads_without_writing_and_a_record_read_reconciles_seed_edits(stored):
    slug = "compatibility"
    page = stored.read_page(slug).replace('"overview": "o"', '"overview": "agent edit"')
    page = page.replace("</body>", "<div>agent page marker</div></body>")
    stored.write_page(slug, page)
    served = ledger_server.page_for(slug)
    assert "agent page marker" not in served
    assert stored.read_page(slug) == page
    assert stored.read_snapshot(slug)["overview"] == "o"
    assert stored.get_document(slug)["overview"] == "agent edit"


def test_page_with_unreadable_json_serves_the_shell_titled_by_its_slug(stored):
    slug = "compatibility"
    page = stored.read_page(slug)
    legacy_core.paths(slug)[1].write_text("broken JSON")
    served = ledger_server.page_for(slug)
    assert f"<title>{slug}</title>" in served
    assert f'<meta name="ledger-token" content="{legacy_core.read_token(page)}">' in served
