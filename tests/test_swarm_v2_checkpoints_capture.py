import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from scripts.swarm_v2 import filesystem
from scripts.swarm_v2.auth_context import Registration
from scripts.swarm_v2.checkpoints import capture, model, native, runtime
from scripts.swarm_v2.supervision_protocol import checkpoint
from tests import sv2_ckp02_cases as source_cases
from tests import sv2_ckp03_cases as native_cases

pytestmark = pytest.mark.unit


@pytest.fixture
def portable(tmp_path, monkeypatch):
    attempt = tmp_path / "e1"
    attempt.mkdir()
    worktree = source_cases.worktree(tmp_path)
    worktree.rename(attempt / "worktrees")
    record = {"attempt": "e1", "layout": filesystem.mapping(filesystem.load()), "homes": {"claude": "homes/claude"}}
    (attempt / "execution.json").write_text(json.dumps(record))
    home = native_cases.home_for("claude", tmp_path, attempt)
    registration = Registration(
        "g", "s1", "e1", 2, "seat", "t1", "account", "brain", ["local:fixture"], "issuer", "worker", "key", ""
    )
    spec = {
        "harness": "claude",
        "registration": asdict(registration),
        "authority": {"execution_id": "e1", "generation": 2},
        "image_digest": "sha256:" + "c" * 64,
    }
    (attempt / "launch.json").write_text(json.dumps(spec))
    environ = {runtime.ROOT: str(tmp_path / "durable"), "CLAUDE_CONFIG_DIR": str(home)}
    scope = {"incarnation": "fixture", "authority": spec["authority"]}
    newest = home / "projects" / native.encoded(attempt) / f"{native_cases.SESSIONS['claude']}.jsonl"
    monkeypatch.setattr(runtime, "version", lambda *args: native_cases.VERSIONS["claude"])
    return attempt, scope, environ, newest


def test_capture_normalizes_record_sequence_kind_and_missing_text(tmp_path, monkeypatch):
    transcript = tmp_path / "conversation"
    records = [{"kind": "user", "text": "Hello\u2028world"}, {"kind": "assistant"}]
    calls = []
    monkeypatch.setattr(capture, "iter_transcript_records", lambda path: calls.append(path) or iter(records))
    raw, count = capture.transcript(transcript)
    assert count == 2
    assert (
        raw
        == (
            '{"seq": 1, "kind": "user", "text": "Hello\u2028world"}\n{"seq": 2, "kind": "assistant", "text": ""}\n'
        ).encode()
    )
    assert calls == [transcript]


@pytest.mark.parametrize(
    "environ, expected",
    [
        ({}, False),
        ({runtime.ROOT: ""}, False),
        ({runtime.ROOT: "/fixture"}, True),
        ({runtime.REMOTE: "true"}, True),
        ({runtime.REMOTE: "TRUE"}, False),
    ],
)
def test_portable_capture_activation(environ, expected):
    assert capture.enabled(environ) is expected


def test_capture_manifest_carries_reconstruction_and_source_authority():
    from datetime import datetime
    from types import SimpleNamespace

    spec = {
        "registration": {
            "execution_id": "e1",
            "task_id": "t1",
            "generation": 3,
            "project_ids": ["github.com/fixture/project"],
        },
        "harness": "claude",
        "image_digest": "sha256:" + "c" * 64,
        "task_revision": 7,
    }
    source = SimpleNamespace(fields=lambda: {"base_commit": "base", "branch_name": "fixture"})
    doc = capture.document(spec, "ckp-one", 5, source)
    created = doc["created_at"]
    assert created.endswith("Z")
    assert datetime.fromisoformat(created).utcoffset().total_seconds() == 0
    assert doc == {
        "schema_version": "2.2",
        "operation_id": "ckp-one",
        "checkpoint_id": "ckp-one",
        "checkpoint_sequence": 5,
        "status": "complete",
        "created_at": created,
        "committed_at": created,
        "authority": {
            "execution_id": "e1",
            "task_id": "t1",
            "task_generation": 3,
            "project_id": "github.com/fixture/project",
        },
        "continuation": "context",
        "native_resume_capability": "unverified",
        "quiescence_method": "stopped_process",
        "transcript_durable_cursor": {"sequence": 0, "byte_offset": 0},
        "reconstruction": {
            "harness": "claude",
            "image_digest": spec["image_digest"],
            "task_revision": 7,
            "execution_generation": 3,
        },
        "dependencies_manifest": {},
        "restore_constraints": ["same qualified harness version"],
        "base_commit": "base",
        "branch_name": "fixture",
    }
    spec.pop("task_revision")
    assert capture.document(spec, "ckp-two", 6, source)["reconstruction"]["task_revision"] == 0


@pytest.mark.parametrize("missing", ["image", "source"])
def test_capture_refuses_incomplete_launch_identity_or_source(portable, missing):
    attempt, scope, environ, newest = portable
    if missing == "image":
        spec = json.loads((attempt / "launch.json").read_text())
        del spec["image_digest"]
        (attempt / "launch.json").write_text(json.dumps(spec))
        reason = "image_identity_absent"
    else:
        (attempt / "worktrees/.git").rename(attempt / "missing-git")
        reason = "source_checkout_absent"
    assert capture.publish(attempt, scope, environ, newest) == {"status": "incomplete", "reason": reason}
    assert not (attempt / "checkpoints").exists()


def test_worker_capture_publishes_and_validates_native_and_source_material(portable, monkeypatch):
    attempt, scope, environ, newest = portable
    calls = []
    original = native.Qualifier.capture

    def qualified(reader, harness, home, session, cwd, version, stopped):
        calls.append((harness, home, session, cwd, version, stopped))
        return original(reader, harness, home, session, cwd, version, stopped)

    def installed(harness, environment):
        assert environment == environ
        return native_cases.VERSIONS[harness]

    monkeypatch.setattr(native.Qualifier, "capture", qualified)
    monkeypatch.setattr(runtime, "version", installed)
    acknowledgement = capture.publish(attempt, scope, environ, newest)
    identifier = checkpoint(attempt, {**scope, **acknowledgement}, scope)
    assert identifier == "ckp-fixture"
    assert acknowledgement["manifest"] == "checkpoints/fixture/manifest.json"
    assert calls == [
        (
            "claude",
            Path(environ["CLAUDE_CONFIG_DIR"]),
            native_cases.SESSIONS["claude"],
            attempt,
            native_cases.VERSIONS["claude"],
            True,
        )
    ]
    assert runtime.validate_export(attempt, identifier, environ)
    spec = json.loads((attempt / "launch.json").read_text())
    store = runtime.worker_store(spec, attempt, environ)
    validator = model.Validator(store)
    assert validator.verify(identifier).continuation == "native"
    manifest = json.loads(store.get_range(store.recorded(identifier)))
    assert manifest["transcript_durable_cursor"]["sequence"] == 2
    assert manifest["checkpoint_sequence"] == 1
    assert manifest["quiescence_method"] == "stopped_process"
    assert set(manifest["inventory"]) >= {"native_session", "normalized_transcript", "source_bundle"}


def test_worker_capture_uses_the_prepared_checkout_and_native_directory(portable):
    attempt, scope, environ, newest = portable
    root = attempt / "worktrees"
    saved = attempt / "source"
    root.rename(saved)
    root.mkdir()
    checkout = root / "task"
    saved.rename(checkout)
    workspace = attempt / "spool" / "workspaces" / "t1.json"
    workspace.parent.mkdir(parents=True)
    workspace.write_text(json.dumps({"path": str(checkout)}))
    spec = json.loads((attempt / "launch.json").read_text())
    spec["authority"]["task_id"] = "t1"
    spec["checkout"] = {"project": "github.com/the-cloud-clockwork/agentihooks", "agent": "engineer-1"}
    (attempt / "launch.json").write_text(json.dumps(spec))
    native_folder = newest.parent.parent / native.encoded(checkout)
    newest.parent.rename(native_folder)
    newest = native_folder / newest.name
    result = capture.publish(attempt, scope, environ, newest)
    assert result["status"] == "complete"
    identifier = checkpoint(attempt, {**scope, **result}, scope)
    store = runtime.worker_store(spec, attempt, environ)
    assert model.Validator(store).verify(identifier).continuation == "native"
    assert not (root / ".git").exists()


def test_corruption_after_export_is_refused_by_the_supervisor_validator(portable):
    attempt, scope, environ, newest = portable
    assert capture.publish(attempt, scope, environ, newest)["status"] == "complete"
    spec = json.loads((attempt / "launch.json").read_text())
    store = runtime.worker_store(spec, attempt, environ)
    manifest = json.loads(store.get_range(store.recorded("ckp-fixture")))
    ref = manifest["inventory"][model.NATIVE_SESSION]
    store.backend.write(store.scope.key("objects", ref["sha256"]), b"corrupt")
    assert runtime.validate_export(attempt, "ckp-fixture", environ) is False


def test_capture_uses_an_existing_private_acknowledgement_folder(portable):
    attempt, scope, environ, newest = portable
    (attempt / "checkpoints" / scope["incarnation"]).mkdir(parents=True)
    acknowledgement = capture.publish(attempt, scope, environ, newest)
    assert acknowledgement["status"] == "complete"
    assert acknowledgement["manifest"] == "checkpoints/fixture/manifest.json"


@pytest.mark.parametrize("failure", ["promotion", "validation"])
def test_refused_publication_or_verification_never_reports_complete(portable, monkeypatch, failure):
    from types import SimpleNamespace

    attempt, scope, environ, newest = portable
    if failure == "promotion":
        monkeypatch.setattr(capture.publisher.Publisher, "publish", lambda *args: SimpleNamespace(promoted=False))
    else:
        publish = capture.publisher.Publisher.publish

        def corrupted(owner, doc, materials):
            promoted = publish(owner, doc, materials)
            monkeypatch.setattr(owner.validator, "verify", lambda checkpoint: SimpleNamespace(authoritative=False))
            return promoted

        monkeypatch.setattr(capture.publisher.Publisher, "publish", corrupted)
    assert capture.publish(attempt, scope, environ, newest) == {
        "status": "incomplete",
        "reason": "checkpoint_validation_refused",
    }
    assert not (attempt / "checkpoints").exists()


def test_capture_revalidates_original_execution_generation_before_acknowledging(portable, monkeypatch):
    attempt, scope, environ, newest = portable
    publish = capture.publisher.Publisher.publish

    def altered(owner, doc, materials):
        promoted = publish(owner, doc, materials)
        from scripts.swarm_v2.artifacts.base import ArtifactRef

        stored = json.loads(owner.store.get_range(owner.store.recorded(doc["checkpoint_id"])))
        stored["authority"]["execution_generation"] += 1
        raw = json.dumps(stored).encode()
        ref = ArtifactRef.of(raw)
        owner.store.backend.write(owner.store.scope.key("objects", ref.sha256), raw)
        record = {
            "sha256": ref.sha256,
            "size": ref.size,
            "execution_id": owner.store.scope.execution_id,
            "generation": owner.store.scope.generation,
        }
        owner.store.backend.write(
            owner.store.scope.key("artifacts", doc["checkpoint_id"] + ".json"), json.dumps(record).encode()
        )
        return promoted

    monkeypatch.setattr(capture.publisher.Publisher, "publish", altered)
    assert capture.publish(attempt, scope, environ, newest) == {
        "status": "incomplete",
        "reason": "checkpoint_validation_refused",
    }


def test_codex_capture_uses_its_native_home_and_rollout_session_suffix(portable, tmp_path, monkeypatch):
    attempt, scope, environ, _ = portable
    spec = json.loads((attempt / "launch.json").read_text())
    spec["harness"] = "codex"
    (attempt / "launch.json").write_text(json.dumps(spec))
    home = native_cases.home_for("codex", tmp_path, attempt)
    environ["CODEX_HOME"] = str(home)
    monkeypatch.setattr(runtime, "version", lambda harness, environment: native_cases.VERSIONS[harness])
    [newest] = list(home.rglob(f"*{native_cases.SESSIONS['codex']}.jsonl"))
    acknowledgment = capture.publish(attempt, scope, environ, newest)
    assert acknowledgment["status"] == "complete"
    store = runtime.worker_store(spec, attempt, environ)
    manifest = json.loads(store.get_range(store.recorded("ckp-fixture")))
    assert manifest["continuation"] == "native"
    assert manifest["native_session_id"] == native_cases.SESSIONS["codex"]
    assert manifest["reconstruction"]["harness"] == "codex"


def test_unsupported_native_version_is_published_only_as_context(portable, monkeypatch):
    attempt, scope, environ, newest = portable
    monkeypatch.setattr(runtime, "version", lambda *args: "9.0.0")
    assert capture.publish(attempt, scope, environ, newest)["status"] == "complete"
    spec = json.loads((attempt / "launch.json").read_text())
    store = runtime.worker_store(spec, attempt, environ)
    verdict = model.Validator(store).verify("ckp-fixture")
    assert verdict.authoritative
    assert verdict.continuation == "context"


def test_capture_keeps_execution_generation_when_the_task_generation_differs(portable, monkeypatch):
    attempt, scope, environ, newest = portable
    spec = json.loads((attempt / "launch.json").read_text())
    store = runtime.granted(Path(environ[runtime.ROOT]), replace(Registration(**spec["registration"]), generation=7))

    def granted(actual, root, environment):
        assert (actual, root, environment) == (spec, attempt, environ)
        return store

    monkeypatch.setattr(runtime, "worker_store", granted)
    assert capture.publish(attempt, scope, environ, newest)["status"] == "complete"
    doc = json.loads(store.get_range(store.recorded("ckp-fixture")))
    assert doc["authority"]["task_generation"] == 7
    assert doc["authority"]["execution_generation"] == 2
    assert doc["reconstruction"]["execution_generation"] == 2
    assert model.Validator(store, execution_generation=2).verify("ckp-fixture").authoritative
