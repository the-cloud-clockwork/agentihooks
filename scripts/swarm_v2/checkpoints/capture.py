import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from hooks.memory.transcript_reader import iter_transcript_records
from scripts.swarm_v2 import filesystem
from scripts.swarm_v2.checkpoints import git_capture, model, native, publisher, redaction, runtime
from scripts.swarm_v2.supervision_protocol import write


def enabled(environ: Mapping[str, str]) -> bool:
    return bool(environ.get(runtime.ROOT)) or environ.get(runtime.REMOTE) == "true"


def transcript(path: Path) -> tuple[bytes, int]:
    records = [
        {"seq": index, "kind": entry.get("kind"), "text": entry.get("text", "")}
        for index, entry in enumerate(iter_transcript_records(path), 1)
    ]
    raw = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records).encode()
    return redaction.scrub(raw)[0], len(records)


def document(spec: dict, checkpoint_id: str, sequence: int, captured: git_capture.SourceCapture) -> dict:
    registration = spec["registration"]
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": "2.2",
        "operation_id": checkpoint_id,
        "checkpoint_id": checkpoint_id,
        "checkpoint_sequence": sequence,
        "status": "complete",
        "created_at": now,
        "committed_at": now,
        "authority": {
            "execution_id": registration["execution_id"],
            "task_id": registration["task_id"],
            "task_generation": registration["generation"],
            "project_id": registration["project_ids"][0],
        },
        "continuation": model.CONTEXT,
        "native_resume_capability": "unverified",
        "quiescence_method": "stopped_process",
        "transcript_durable_cursor": {"sequence": 0, "byte_offset": 0},
        "reconstruction": {
            "harness": spec["harness"],
            "image_digest": spec["image_digest"],
            "task_revision": spec.get("task_revision", 0),
            "execution_generation": registration["generation"],
        },
        "dependencies_manifest": {},
        "restore_constraints": ["same qualified harness version"],
        **captured.fields(),
    }


def publish(attempt: Path, scope: dict, environ: Mapping[str, str], newest: Path) -> dict:
    spec = json.loads((attempt / "launch.json").read_text())
    if "image_digest" not in spec:
        return {"status": "incomplete", "reason": "image_identity_absent"}
    store = runtime.worker_store(spec, attempt, environ)
    qualifier = native.Qualifier(model.Validator(store, execution_generation=spec["registration"]["generation"]))
    record = json.loads((attempt / "execution.json").read_text())
    execution = filesystem.recorded(attempt.resolve(), record)
    worktree, cwd = runtime.source_paths(spec, execution)
    if not (worktree / ".git").exists():
        return {"status": "incomplete", "reason": "source_checkout_absent"}
    source = git_capture.capture(worktree, "origin/dev")
    sequence = publisher.Publisher(qualifier.validator).sequence() + 1
    checkpoint_id = f"ckp-{scope['incarnation']}"
    doc = document(spec, checkpoint_id, sequence, source)
    doc["authority"]["task_generation"] = store.scope.generation
    doc["authority"]["execution_generation"] = spec["registration"]["generation"]
    text, records = transcript(newest)
    doc["transcript_durable_cursor"] = {"sequence": records, "byte_offset": len(text)}
    harness = spec["harness"]
    variable = "CLAUDE_CONFIG_DIR" if harness == "claude" else "CODEX_HOME"
    taken = qualifier.capture(
        harness, Path(environ[variable]), newest.stem[-36:], cwd, runtime.version(harness, environ), stopped=True
    )
    doc = native.merged(doc, taken)
    materials = {**source.materials, model.TRANSCRIPT: text}
    if isinstance(taken, native.NativeCapture):
        materials[model.NATIVE_SESSION] = taken.material()
    promoted = publisher.Publisher(qualifier.validator).publish(doc, materials)
    if not promoted.promoted or not qualifier.validator.verify(checkpoint_id).authoritative:
        return {"status": "incomplete", "reason": "checkpoint_validation_refused"}
    manifest = Path("checkpoints") / scope["incarnation"] / "manifest.json"
    (attempt / manifest).parent.mkdir(parents=True, exist_ok=True)
    write(attempt / manifest, {**scope, "status": "complete", "checkpoint_id": checkpoint_id})
    digest = hashlib.sha256((attempt / manifest).read_bytes()).hexdigest()
    return {"status": "complete", "manifest": str(manifest), "sha256": digest}
