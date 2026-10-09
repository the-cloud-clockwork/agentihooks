import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))

import ledger_core as core
import ledger_hook
import ledger_media as media
import ledger_server
import new_ledger
import watch_ledger

from scripts.inbox.store import InboxStore
from scripts.swarm.store import RedisStore, SwarmConfig
from tests.swarm_ledger import legacy_page  # noqa: E402

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG = "image-delivery"
GIF = b"GIF89a\x01\x00\x01\x00"


@pytest.fixture
def delivery(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    import fakeredis

    box = InboxStore(fakeredis.FakeRedis(decode_responses=True))
    RedisStore(box.redis).create(SwarmConfig(SLUG, "/repo", 1, 0))
    monkeypatch.setattr("scripts.inbox.store.connect", lambda environ=None: box)
    content = {"title": "Images", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    html, _ = core.paths(SLUG)
    html.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765))
    core.sync(SLUG)
    attachments = [media.store(SLUG, GIF), media.store(SLUG, GIF + b"another")]
    paths = [str(media.path_of(SLUG, att["id"]).resolve()) for att in attachments]
    return attachments, paths


@pytest.mark.parametrize("thread", ["chat", "phases/p1/comments"])
@pytest.mark.parametrize("surface", ["inbox", "watch", "hook"])
def test_each_delivery_surface_names_every_image_and_harness_tool(delivery, thread, surface):
    attachments, paths = delivery
    state, rejected = core.sync(
        SLUG, ops=[{"op": "add", "thread": thread, "id": "picture", "text": "", "attachments": attachments}]
    )
    assert rejected == []
    [event] = [e for e in state["_meta"]["events"] if e.get("id") == "picture"]
    if surface == "inbox":
        [item] = ledger_server.relay_to_inbox(SLUG, state)
        text = item.text
    elif surface == "watch":
        text = watch_ledger.line(event)
    else:
        text = ledger_hook.context_text({"slug": SLUG, "name": "engineer"}, [event])
    for path in paths:
        assert path in text
        assert Path(path).is_absolute()
    assert "Claude" in text and "Read" in text
    assert "Codex" in text and "view_image" in text
    assert "open" in text.lower()
    entry = state["chat"][0] if thread == "chat" else state["phases"][0]["comments"][0]
    assert entry["attachments"] == attachments


@pytest.mark.parametrize("op", ["edit", "delete"])
def test_an_edited_or_deleted_entry_keeps_its_image_paths(delivery, op):
    attachments, paths = delivery
    core.sync(SLUG, ops=[{"op": "add", "thread": "chat", "id": "picture", "text": "look", "attachments": attachments}])
    state, _ = core.sync(SLUG, ops=[{"op": op, "thread": "chat", "id": "picture", "text": "look again"}])
    event = state["_meta"]["events"][-1]
    text = watch_ledger.line(event)
    assert all(path in text for path in paths)


def test_a_plain_event_keeps_its_existing_text():
    event = {"rev": 2, "by": "operator", "kind": "message added", "target": "chat", "id": "plain", "text": "hello"}
    assert watch_ledger.line(event) == 'OPERATOR rev=2 message added on chat [plain]: "hello"'
