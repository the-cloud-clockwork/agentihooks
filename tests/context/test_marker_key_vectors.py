"""The outbox drain derives the idempotency key each vector shared with agentibrain-kernel names."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hooks._brain_http as brain_http
from hooks.context.brain_writer_hook import _drain_outbox

VECTORS = json.loads((Path(__file__).parents[1] / "fixtures/marker-idempotency-vectors.json").read_text())["vectors"]


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
