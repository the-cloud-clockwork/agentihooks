"""Personal and swarm sessions on one host: each sees only its own brain's feed and drains only its own markers."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

import hooks._brain_http as brain_http
from hooks.context import brain_adapter, broadcast
from hooks.context import brain_writer_hook as writer
from hooks.context.brain_adapter import BrainEntry
from scripts.swarm import brain_binding

PERSONAL = "https://brain.personal.example"
SWARM = "https://brain.swarm.example"
LESSON = "Pin the shard budget because two brains may learn the same words."
MARKER = {"type": "lesson", "content": LESSON, "attrs": {}}


class Brains:
    def __init__(self):
        self.received: dict[str, list[tuple[str, str]]] = {PERSONAL: [], SWARM: []}
        self.down: set[str] = set()
        self.feeds: dict[str, list[BrainEntry]] = {}

    def post(self, path, body=None, idempotency_key=None, **kw):
        from hooks.config import BRAIN_URL

        if BRAIN_URL in self.down:
            return None
        self.received[BRAIN_URL].append((body["content"], idempotency_key))
        return {"ok": True}

    def fetch(self):
        from hooks.config import BRAIN_URL

        return [BrainEntry(e.id, e.title, e.content) for e in self.feeds[BRAIN_URL]]


@pytest.fixture
def brains(monkeypatch, tmp_path):
    found = Brains()
    monkeypatch.setattr(brain_http, "post", found.post)
    monkeypatch.setattr("hooks.config.reload_brain_env", lambda force=False: {})
    monkeypatch.setattr(broadcast, "BASE_CHANNELS", ("brain", "amygdala"))
    monkeypatch.setattr(brain_adapter, "_get_source", lambda: type("Source", (), {"fetch": lambda s: found.fetch()})())
    found.outbox = tmp_path / "brain-outbox"
    return found


@pytest.fixture
def same_names(monkeypatch):
    moment = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(writer, "datetime", type("Fixed", (), {"now": staticmethod(lambda tz=None: moment)}))
    monkeypatch.setattr(writer, "uuid", type("Uuid", (), {"uuid4": staticmethod(lambda: uuid.UUID(int=7))}))


def client(monkeypatch, url, *, swarm=False, reference="SWARM_BRAIN_API_TOKEN", token="first"):
    monkeypatch.setattr("hooks.config.BRAIN_URL", url)
    monkeypatch.setattr("hooks.config.BRAIN_HTTP_TOKEN", token)
    if swarm:
        binding = {"status": brain_binding.BOUND, "api": url, "api_env": reference}
        monkeypatch.setenv(brain_binding.VARIABLE, json.dumps(binding))
    else:
        monkeypatch.delenv(brain_binding.VARIABLE, raising=False)


def feed_seen(session: str) -> list[str]:
    return [
        m["channel"] + "|" + m["message"]
        for m in broadcast.get_pending_broadcasts(session)
        if m.get("source") == "brain-adapter"
    ]


def publish(brains, monkeypatch, url, *, swarm=False):
    client(monkeypatch, url, swarm=swarm)
    brain_adapter.force_refresh()


def two_feeds(brains):
    brains.feeds[PERSONAL] = [BrainEntry("lessons", "Lessons", LESSON), BrainEntry("mine", "Mine", "personal only")]
    brains.feeds[SWARM] = [BrainEntry("lessons", "Lessons", LESSON), BrainEntry("ours", "Ours", "swarm only")]


def test_each_client_receives_only_its_own_brain_feed(brains, monkeypatch):
    two_feeds(brains)
    publish(brains, monkeypatch, PERSONAL)
    publish(brains, monkeypatch, SWARM, swarm=True)

    client(monkeypatch, PERSONAL)
    personal = feed_seen("personal-session")
    client(monkeypatch, SWARM, swarm=True)
    swarm = feed_seen("swarm-session")

    assert len(personal) == len(swarm) == 2
    assert any("personal only" in m for m in personal) and not any("swarm only" in m for m in personal)
    assert any("swarm only" in m for m in swarm) and not any("personal only" in m for m in swarm)
    assert {m.split("|")[0] for m in personal}.isdisjoint({m.split("|")[0] for m in swarm})


def test_a_source_change_reconciles_only_its_own_brain(brains, monkeypatch):
    two_feeds(brains)
    publish(brains, monkeypatch, PERSONAL)
    publish(brains, monkeypatch, SWARM, swarm=True)
    client(monkeypatch, SWARM, swarm=True)
    before = feed_seen("swarm-session")

    brains.feeds[PERSONAL] = [BrainEntry("mine", "Mine", "personal changed")]
    publish(brains, monkeypatch, PERSONAL)

    client(monkeypatch, SWARM, swarm=True)
    assert feed_seen("swarm-session") == before
    client(monkeypatch, PERSONAL)
    assert [m for m in feed_seen("personal-session") if "personal changed" in m]
    assert not [m for m in feed_seen("personal-session") if LESSON in m]


def test_a_route_change_never_republishes_the_old_brain_feed(brains, monkeypatch):
    two_feeds(brains)
    publish(brains, monkeypatch, PERSONAL)
    publish(brains, monkeypatch, SWARM, swarm=True)

    publish(brains, monkeypatch, SWARM)

    client(monkeypatch, SWARM, swarm=True)
    assert not any("personal only" in m for m in feed_seen("swarm-session"))
    client(monkeypatch, PERSONAL)
    assert any("personal only" in m for m in feed_seen("personal-session"))


def test_a_wildcard_subscriber_never_reads_another_brain_feed(brains, monkeypatch):
    two_feeds(brains)
    publish(brains, monkeypatch, PERSONAL)
    publish(brains, monkeypatch, SWARM, swarm=True)
    monkeypatch.setattr(broadcast, "BASE_CHANNELS", ("*",))

    client(monkeypatch, SWARM, swarm=True)

    assert not any("personal only" in m for m in feed_seen("swarm-session"))
    assert any("swarm only" in m for m in feed_seen("swarm-session"))


def test_fleet_emergency_channels_reach_every_brain_audience(brains, monkeypatch):
    two_feeds(brains)
    publish(brains, monkeypatch, PERSONAL)
    broadcast.create_broadcast("fleet halt", severity="critical", channel="amygdala")
    broadcast.create_broadcast("fleet notice", severity="info")

    for url, swarm in ((PERSONAL, False), (SWARM, True), ("", True)):
        client(monkeypatch, url, swarm=swarm)
        messages = [m["message"] for m in broadcast.get_pending_broadcasts(f"session-{url}-{swarm}")]
        assert "fleet halt" in messages and "fleet notice" in messages


def test_overlapping_outbox_files_drain_only_into_their_own_brain(brains, same_names, monkeypatch):
    client(monkeypatch, PERSONAL)
    writer._write_to_outbox([MARKER], "personal-session", str(brains.outbox))
    client(monkeypatch, SWARM, swarm=True)
    writer._write_to_outbox([MARKER], "swarm-session", str(brains.outbox))
    names = sorted(f.name for f in brains.outbox.rglob("*.json"))
    assert len(names) == 2 and names[0] == names[1]

    assert writer._drain_outbox(str(brains.outbox)) == 1
    assert [content for content, _ in brains.received[SWARM]] == [LESSON]
    assert brains.received[PERSONAL] == []

    client(monkeypatch, PERSONAL)
    assert writer._drain_outbox(str(brains.outbox)) == 1
    assert [content for content, _ in brains.received[PERSONAL]] == [LESSON]
    assert len(brains.received[SWARM]) == 1


def test_unbound_legacy_markers_stay_on_the_personal_path(brains, monkeypatch):
    brains.outbox.mkdir()
    legacy = {"type": "lesson", "content": "legacy", "attrs": {}, "session_id": "old"}
    (brains.outbox / "20260101T000000-lesson-legacy.json").write_text(json.dumps(legacy))

    client(monkeypatch, SWARM, swarm=True)
    assert writer._drain_outbox(str(brains.outbox)) == 0
    assert brains.received[SWARM] == []

    client(monkeypatch, PERSONAL)
    assert writer._drain_outbox(str(brains.outbox)) == 1
    assert [content for content, _ in brains.received[PERSONAL]] == ["legacy"]


def test_an_unavailable_brain_recovers_once_into_the_original_audience(brains, monkeypatch):
    brains.down.add(SWARM)
    client(monkeypatch, SWARM, swarm=True)
    writer._write_to_outbox([MARKER], "swarm-session", str(brains.outbox))
    assert writer._drain_outbox(str(brains.outbox)) == 0

    client(monkeypatch, PERSONAL)
    assert writer._drain_outbox(str(brains.outbox)) == 0
    assert brains.received[PERSONAL] == []

    brains.down.clear()
    client(monkeypatch, SWARM, swarm=True)
    assert writer._drain_outbox(str(brains.outbox)) == 1
    assert writer._drain_outbox(str(brains.outbox)) == 0
    assert [content for content, _ in brains.received[SWARM]] == [LESSON]


def test_a_swapped_route_never_drains_another_brain_backlog(brains, monkeypatch):
    brains.down.add(SWARM)
    client(monkeypatch, SWARM, swarm=True)
    writer._write_to_outbox([MARKER], "swarm-session", str(brains.outbox))
    brains.down.clear()

    client(monkeypatch, PERSONAL, swarm=True)
    assert writer._drain_outbox(str(brains.outbox)) == 0
    assert brains.received[PERSONAL] == []

    client(monkeypatch, SWARM, swarm=True)
    assert writer._drain_outbox(str(brains.outbox)) == 1
    assert len(brains.received[SWARM]) == 1


def test_a_key_rotation_replays_once_with_the_recorded_key(brains, monkeypatch):
    client(monkeypatch, SWARM, swarm=True, token="first")
    brains.down.add(SWARM)
    writer._write_to_outbox([MARKER], "swarm-session", str(brains.outbox))
    [written] = [json.loads(f.read_text()) for f in brains.outbox.rglob("*.json")]
    brains.down.clear()

    client(monkeypatch, SWARM, swarm=True, token="rotated")
    assert writer._drain_outbox(str(brains.outbox)) == 1
    assert brains.received[SWARM] == [(LESSON, written["idempotency_key"])]


def test_a_changed_client_reference_never_replays_the_old_reference_markers(brains, monkeypatch):
    client(monkeypatch, SWARM, swarm=True)
    brains.down.add(SWARM)
    writer._write_to_outbox([MARKER], "swarm-session", str(brains.outbox))
    brains.down.clear()

    client(monkeypatch, SWARM, swarm=True, reference="SWARM_BRAIN_API_TOKEN_NEXT")
    assert writer._drain_outbox(str(brains.outbox)) == 0
    assert brains.received[SWARM] == []
