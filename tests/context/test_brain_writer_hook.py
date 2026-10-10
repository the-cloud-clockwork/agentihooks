"""brain_writer_hook — outbox buffer + HTTP drain behavior.

Covers the shared-outbox race (concurrent drains of the same file must not
crash the caller), quarantine of unparseable files, refusal retention, and
idempotency-key parity between a live POST and its outbox replay.
"""

from __future__ import annotations

import json

import pytest

import hooks._brain_http as brain_http
from hooks.context.brain_writer_hook import (
    _drain_outbox,
    _marker_request,
    _write_to_outbox,
)

MARKER = {"type": "lesson", "content": "test lesson", "attrs": {"scope": "x"}}


@pytest.fixture
def http_ok(monkeypatch):
    """Stub the HTTP layer to accept every POST, recording calls."""
    calls: list[tuple[dict, str]] = []
    monkeypatch.setattr(brain_http, "brain_http_enabled", lambda: True)
    monkeypatch.setattr(
        brain_http,
        "post",
        lambda path, body=None, idempotency_key=None, **kw: calls.append((body, idempotency_key)) or {"ok": True},
    )
    return calls


def test_drain_posts_and_deletes(tmp_path, http_ok):
    _write_to_outbox([MARKER], "sess-1", str(tmp_path))
    assert _drain_outbox(str(tmp_path)) == 1
    assert not list(tmp_path.glob("*.json"))


def test_drain_key_matches_live_post_key(tmp_path, http_ok):
    _write_to_outbox([MARKER], "sess-1", str(tmp_path))
    _drain_outbox(str(tmp_path))
    _, live_key = _marker_request(MARKER, "sess-1")
    assert http_ok[0][1] == live_key


@pytest.mark.parametrize(
    ("folders", "expected"),
    [({"cwd": "/w", "project": "/p"}, "/w"), ({"project": "/p"}, "/p"), ({}, "")],
)
def test_drain_resolves_scope_from_the_recorded_folder(tmp_path, http_ok, monkeypatch, folders, expected):
    import hooks.context.brain_writer_hook as hook

    (tmp_path / "a.json").write_text(json.dumps({**MARKER, "session_id": "sess-1", **folders}))
    seen = []
    monkeypatch.setattr(hook, "_marker_request", lambda marker, session_id, cwd: seen.append(cwd) or ({}, "k"))

    assert _drain_outbox(str(tmp_path)) == 1
    assert seen == [expected]


def test_drain_survives_file_vanishing_after_post(tmp_path, monkeypatch):
    """A concurrent drain unlinking the file first must not raise."""
    _write_to_outbox([MARKER], "sess-1", str(tmp_path))

    def post_and_steal(path, body=None, idempotency_key=None, **kw):
        for f in tmp_path.glob("*.json"):
            f.unlink()
        return {"ok": True}

    monkeypatch.setattr(brain_http, "brain_http_enabled", lambda: True)
    monkeypatch.setattr(brain_http, "post", post_and_steal)
    assert _drain_outbox(str(tmp_path)) == 1
    assert not list(tmp_path.glob("*.json"))


def test_drain_quarantines_unparseable(tmp_path, http_ok):
    (tmp_path / "aa-broken.json").write_text("{not json")
    assert _drain_outbox(str(tmp_path)) == 0
    assert (tmp_path / "aa-broken.json.bad").exists()
    assert not list(tmp_path.glob("*.json"))


def test_drain_keeps_files_on_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(brain_http, "brain_http_enabled", lambda: True)
    monkeypatch.setattr(brain_http, "post", lambda *a, **k: None)
    _write_to_outbox([MARKER], "sess-1", str(tmp_path))
    assert _drain_outbox(str(tmp_path)) == 0
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_drain_noop_when_http_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(brain_http, "brain_http_enabled", lambda: False)
    _write_to_outbox([MARKER], "sess-1", str(tmp_path))
    assert _drain_outbox(str(tmp_path)) == 0
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_outbox_payload_preserves_original_content(tmp_path, http_ok):
    _write_to_outbox([MARKER], "sess-1", str(tmp_path))
    payload = json.loads(next(iter(tmp_path.glob("*.json"))).read_text())
    assert payload["type"] == MARKER["type"]
    assert payload["content"] == MARKER["content"]
    assert payload["session_id"] == "sess-1"


def test_drain_sweeps_backlog_sibling(tmp_path, http_ok):
    """Markers parked in <outbox>-backlog (SSH-era orphans) self-deliver."""
    outbox = tmp_path / "brain-outbox"
    outbox.mkdir()
    backlog = tmp_path / "brain-outbox-backlog"
    backlog.mkdir()
    (backlog / "old.json").write_text(
        json.dumps(
            {
                "type": "milestone",
                "content": "stranded since may",
                "attrs": {},
                "session_id": "sess-old",
                "ts": "2026-05-12T19:27:10+00:00",
            }
        )
    )
    _write_to_outbox([MARKER], "sess-1", str(outbox))
    assert _drain_outbox(str(outbox)) == 2
    assert not list(backlog.glob("*.json"))
    assert not list(outbox.glob("*.json"))


def test_drain_forwards_original_ts_in_attrs(tmp_path, http_ok):
    """Replay must carry the file's ts so brain-api backdates the marker."""
    (tmp_path / "buf.json").write_text(
        json.dumps(
            {
                "type": "lesson",
                "content": "carry my timestamp",
                "attrs": {},
                "session_id": "s",
                "ts": "2026-05-12T19:27:10+00:00",
            }
        )
    )
    assert _drain_outbox(str(tmp_path)) == 1
    body, _ = http_ok[0]
    assert body["attrs"]["ts"] == "2026-05-12T19:27:10+00:00"


def test_drain_backlog_only_no_outbox_dir(tmp_path, http_ok):
    """A machine with only the orphaned backlog (outbox never recreated)."""
    outbox = tmp_path / "brain-outbox"  # deliberately not created
    backlog = tmp_path / "brain-outbox-backlog"
    backlog.mkdir()
    (backlog / "one.json").write_text(json.dumps({"type": "lesson", "content": "orphan", "session_id": "s"}))
    assert _drain_outbox(str(outbox)) == 1
    assert not list(backlog.glob("*.json"))


def test_drain_quarantines_poison_pill_and_continues(tmp_path, monkeypatch):
    """One server-rejected (400) marker must not wedge files sorted after it."""
    monkeypatch.setattr(brain_http, "brain_http_enabled", lambda: True)

    def post(path, body=None, idempotency_key=None, **kw):
        if "poison" in body["content"]:
            return {"__http_status__": 400}
        return {"ok": True}

    monkeypatch.setattr(brain_http, "post", post)
    _write_to_outbox([{"type": "lesson", "content": "AAA poison", "attrs": {}}], "s", str(tmp_path))
    files = sorted(tmp_path.glob("*.json"))
    files[0].rename(tmp_path / "00000000T000000-lesson-aaaaaaaa.json")  # sorts first
    _write_to_outbox([{"type": "lesson", "content": "good marker", "attrs": {}}], "s", str(tmp_path))

    assert _drain_outbox(str(tmp_path)) == 1
    assert not list(tmp_path.glob("*.json"))
    assert len(list(tmp_path.glob("*.bad"))) == 1


def test_drain_stops_without_quarantine_on_credential_error(tmp_path, monkeypatch):
    """401 (stale token) must leave every file intact for the next run."""
    monkeypatch.setattr(brain_http, "brain_http_enabled", lambda: True)
    monkeypatch.setattr(
        brain_http, "post", lambda path, body=None, idempotency_key=None, **kw: {"__http_status__": 401}
    )
    _write_to_outbox([MARKER, MARKER], "s", str(tmp_path))
    assert _drain_outbox(str(tmp_path)) == 0
    assert len(list(tmp_path.glob("*.json"))) == 2
    assert not list(tmp_path.glob("*.bad"))


def test_idem_key_truncates_before_hashing():
    """Key parity with kernel drains for >4096-char content: all three
    implementations hash the TRUNCATED content."""
    import uuid as _uuid

    long_marker = {"type": "lesson", "content": "x" * 5000, "attrs": {}}
    _, idem = _marker_request(long_marker, "s1")
    expected = _uuid.uuid5(_uuid.NAMESPACE_URL, f"s1-lesson-{'x' * 4096}").hex[:32]
    assert idem == expected


def test_parse_attrs_drops_reserved_ts():
    """Transcript-authored ts= must never reach brain-api's backdating path."""
    from hooks.context.brain_writer_hook import _find_markers

    text = '<!--@lesson ts="2020-01-01T00:00:00Z" scope=x-->backdate me<!--@/lesson-->'
    markers = _find_markers(text)
    assert markers[0]["attrs"] == {"scope": "x"}
