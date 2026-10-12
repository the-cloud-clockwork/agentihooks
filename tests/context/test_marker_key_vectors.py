"""The outbox drain derives the idempotency key each vector shared with agentibrain-kernel names."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import hooks._brain_http as brain_http
from hooks.context.brain_writer_hook import _drain_outbox

VECTORS_FILE = Path(__file__).parents[1] / "fixtures/marker-idempotency-vectors.json"
VECTORS_SHA256 = "17dce5bb059043e1df403806e910101b11a7bac1a7399feaf0656d76518789a8"
VECTORS = json.loads(VECTORS_FILE.read_text())["vectors"]


def test_vectors_file_is_the_pinned_copy_shared_with_the_brain_kernel():
    assert hashlib.sha256(VECTORS_FILE.read_bytes()).hexdigest() == VECTORS_SHA256


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_drain_key_matches_vector(vector, tmp_path, monkeypatch):
    entry = {
        "type": vector["type"],
        "content": vector["content"] * vector["repeat"],
        "session_id": vector["session_id"],
    }
    if "recorded" in vector:
        entry["idempotency_key"] = vector["recorded"]
    (tmp_path / "a.json").write_text(json.dumps(entry))
    keys = []
    monkeypatch.setattr(brain_http, "brain_http_enabled", lambda: True)
    monkeypatch.setattr(
        brain_http,
        "post",
        lambda path, body=None, idempotency_key=None, **kw: keys.append(idempotency_key) or {"ok": True},
    )

    assert _drain_outbox(str(tmp_path)) == 1
    assert keys == [vector["key"]]
