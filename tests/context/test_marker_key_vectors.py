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


@pytest.mark.parametrize("drift", [False, True])
def test_vector_check_accepts_kernel_copy_and_refuses_drift(tmp_path, capsys, drift):
    from scripts import ci_marker_vectors

    kernel = tmp_path / "kernel-vectors.json"
    content = VECTORS_FILE.read_bytes()
    kernel.write_bytes(content + b" " if drift else content)
    assert ci_marker_vectors.main([str(kernel)]) == int(drift)
    expected = (
        "::error::Marker key vectors differ from kernel dev.\n" if drift else "Marker key vectors match kernel dev.\n"
    )
    assert capsys.readouterr().out == expected
    assert kernel.read_bytes() == (content + b" " if drift else content)
    assert VECTORS_FILE.read_bytes() == content


def test_vector_check_missing_authority_fails(tmp_path):
    from scripts import ci_marker_vectors

    with pytest.raises(FileNotFoundError):
        ci_marker_vectors.main([str(tmp_path / "missing.json")])


def test_vector_check_uses_its_own_repository_from_another_folder(tmp_path, monkeypatch, capsys):
    from scripts import ci_marker_vectors

    root = tmp_path / "repo"
    script = root / "scripts/ci_marker_vectors.py"
    script.parent.mkdir(parents=True)
    script.touch()
    local = root / "tests/fixtures/marker-idempotency-vectors.json"
    local.parent.mkdir(parents=True)
    local.write_bytes(VECTORS_FILE.read_bytes())
    kernel = tmp_path / "kernel.json"
    kernel.write_bytes(local.read_bytes())
    monkeypatch.setattr(ci_marker_vectors, "__file__", str(script))
    monkeypatch.chdir(tmp_path)
    assert ci_marker_vectors.main([str(kernel)]) == 0
    assert capsys.readouterr().out == "Marker key vectors match kernel dev.\n"


def test_vector_check_is_on_the_required_tests_path():
    import yaml

    root = VECTORS_FILE.parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/test.yml").read_text())
    assert "lint" in workflow["jobs"]["gate-required"]["needs"]
    steps = workflow["jobs"]["lint"]["steps"]
    checkout = next(
        step for step in steps if step.get("with", {}).get("repository") == "the-cloud-clockwork/agentibrain-kernel"
    )
    assert checkout["uses"] == "actions/checkout@v4"
    assert checkout["with"] == {
        "repository": "the-cloud-clockwork/agentibrain-kernel",
        "ref": "dev",
        "path": ".marker-kernel",
        "persist-credentials": False,
    }
    command = next(
        step
        for step in steps
        if step.get("run")
        == "python -m scripts.ci_marker_vectors .marker-kernel/tests/fixtures/marker-idempotency-vectors.json"
    )
    assert command["if"] == "${{ !cancelled() }}"
    assert steps.index(checkout) < steps.index(command)
    assert "schedule" not in workflow["on" if "on" in workflow else True]


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
