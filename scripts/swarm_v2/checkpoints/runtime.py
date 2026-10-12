import json
import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

from scripts.swarm_v2 import filesystem
from scripts.swarm_v2.artifacts.base import ArtifactRef, ArtifactStore, Backend
from scripts.swarm_v2.artifacts.local import LocalBackend
from scripts.swarm_v2.auth_context import Registration
from scripts.swarm_v2.checkpoints import git_capture, model, native, restore

ROOT = "AGENTIHOOKS_CHECKPOINT_ROOT"
REMOTE = "AGENTIHOOKS_CHECKPOINT_REMOTE"


def granted(root: Path | Backend, registration: Registration) -> ArtifactStore:
    backend = LocalBackend(root) if isinstance(root, Path) else root
    return ArtifactStore(
        backend,
        lambda token: registration if token == registration.grant_id else None,
        registration.grant_id,
    )


def configured(environ: Mapping[str, str]) -> Path | Backend | None:
    if environ.get("AGENTIHOOKS_CHECKPOINT_BUCKET"):
        from scripts.swarm_v2.artifacts.remote import controller_backend

        return controller_backend(environ)
    value = environ.get(ROOT)
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise restore.RestoreRefused("checkpoint root must be absolute")
    return path


def historical(registration: Registration | None, journal: list[dict]) -> Registration | None:
    if registration is None:
        return None
    matches = [
        entry["claim"]
        for entry in journal
        if entry.get("claim", {}).get("execution_id") == registration.execution_id
        and entry["claim"].get("execution_generation") == registration.generation
    ]
    return replace(registration, generation=matches[-1]["generation"]) if matches else None


def worker_store(spec: dict, attempt: Path, environ: Mapping[str, str]) -> ArtifactStore:
    if environ.get(REMOTE) == "true":
        from scripts.swarm_v2.artifacts.remote import worker_store as remote_store

        return remote_store(spec, attempt, environ)
    root = configured(environ)
    if root is None:
        raise restore.RestoreRefused(restore.STORE_MISSING)
    registration = Registration(**spec["registration"])
    authority = spec["authority"]
    if any(getattr(registration, key) != value for key, value in authority.items()):
        raise restore.RestoreRefused("checkpoint registration does not match launch authority")
    return granted(root, registration)


def version(harness: str, environ: Mapping[str, str]) -> str:
    return subprocess.run(
        [harness, "--version"], env=dict(environ), capture_output=True, text=True, timeout=10, check=True
    ).stdout.strip()


def source(validator: model.Validator, checkpoint_id: str, target: Path) -> model.Verdict:
    doc = json.loads(validator.store.get_range(validator.store.recorded(checkpoint_id)))
    if not (target / ".git").exists():
        project = doc["authority"]["project_id"]
        if not project.startswith("github.com/"):
            raise git_capture.SourceError("source checkout is unavailable")
        subprocess.run(
            ["git", "clone", f"https://{project}.git", str(target)],
            capture_output=True,
            timeout=120,
            check=True,
        )
    return git_capture.restore(validator, checkpoint_id, target)


def validate_export(attempt: Path, identifier: str, environ: Mapping[str, str]) -> bool:
    if not environ.get(ROOT) and environ.get(REMOTE) != "true":
        return False
    spec = json.loads((attempt / "launch.json").read_text())
    return model.Validator(worker_store(spec, attempt, environ)).verify(identifier).authoritative


def restoring(store: ArtifactStore, directive: restore.Directive, paths: restore.Paths) -> restore.Restored:
    restorer = restore.Restorer(native.Qualifier(model.Validator(store)), source)
    return restorer.restore(directive, paths.worktree, paths.home, paths.cwd, paths.version_text)


def prepare(spec: dict, attempt: Path, environ: Mapping[str, str] = os.environ) -> restore.Restored | None:
    if "restore" not in spec:
        return None
    directive = restore.Directive.parse(spec["restore"])
    if directive.recovery == restore.FRESH:
        return restore.launched(spec)
    store = worker_store(spec, attempt, environ)
    validator = model.Validator(store)
    verdict = validator.verify(directive.checkpoint_id)
    if not verdict.authoritative:
        raise restore.RestoreRefused(verdict.reason)
    doc = json.loads(store.get_range(store.recorded(directive.checkpoint_id)))
    if doc["authority"]["project_id"] not in spec["registration"]["project_ids"]:
        raise restore.RestoreRefused("checkpoint project is not granted to this worker")
    record = json.loads((attempt / "execution.json").read_text())
    execution = filesystem.recorded(attempt.resolve(), record)
    harness = spec["harness"]
    home = execution.path("home") / harness / (".claude" if harness == "claude" else ".codex")
    paths = restore.Paths(execution.path("worktree"), home, attempt, version(harness, environ))
    directive = restore.Directive.parse(spec["restore"])
    restored = restoring(store, directive, paths)
    if restored.recovery == restore.RECONSTRUCTED:
        doc = json.loads(store.get_range(store.recorded(directive.checkpoint_id)))
        material = doc["inventory"][model.TRANSCRIPT]
        text = store.get_range(ArtifactRef(**material)).decode()
        target = attempt / "continuation.txt"
        target.write_text("Continue from this recorded conversation. Native resume was unavailable.\n" + text)
        target.chmod(0o600)
    return restored


def command(attempt: Path, original: tuple[str, ...]) -> tuple[str, ...]:
    path = attempt / "restore.json"
    if not path.exists():
        return original
    restored = json.loads(path.read_text())
    if restored["recovery"] == restore.NATIVE:
        return tuple(restored["command"])
    if restored["recovery"] == restore.RECONSTRUCTED:
        return (*original, (attempt / "continuation.txt").read_text())
    return original


class Checkpoints:
    def __init__(
        self,
        root: Path | Backend | None,
        registered: Callable[[str], Registration | None],
        compatibility: str,
        execution_generation: Callable[[str], int | None] = lambda execution_id: None,
    ) -> None:
        self.root, self.registered, self.compatibility = root, registered, compatibility
        self.execution_generation = execution_generation

    def validator(self, execution_id: str) -> model.Validator | None:
        if self.root is None:
            return None
        registration = self.registered(execution_id)
        if registration is None:
            return None
        return model.Validator(
            granted(self.root, registration), execution_generation=self.execution_generation(execution_id)
        )

    def list(self, execution_id: str) -> "list[dict]":
        validator = self.validator(execution_id)
        if validator is None:
            return []
        store = validator.store
        documents = []
        for key in store.backend.keys(store.scope.key("artifacts") + "/"):
            identifier = Path(key).stem
            verdict = validator.verify(identifier)
            if verdict.authoritative:
                doc = json.loads(store.get_range(store.recorded(identifier)))
                documents.append(doc)
        generations = [doc["authority"]["task_generation"] for doc in documents]
        for key in store.backend.keys(store.scope.key("pointers") + "/"):
            generations.append(int(Path(key).parent.name))
        newest = max(generations, default=0)
        return [
            {
                "checkpoint_id": doc["checkpoint_id"],
                "sequence": doc["checkpoint_sequence"],
                "status": "complete",
                "compatibility": self.compatibility,
                "continuation": doc["continuation"],
                "watermark": doc["transcript_durable_cursor"]["sequence"],
            }
            for doc in documents
            if doc["authority"]["task_generation"] == newest and doc["authority"]["execution_id"] == execution_id
        ]

    def fetch(self, execution_id: str, checkpoint_id: str) -> dict:
        if checkpoint_id not in {entry["checkpoint_id"] for entry in self.list(execution_id)}:
            raise restore.RestoreRefused("checkpoint is not verified for this execution")
        validator = self.validator(execution_id)
        store = validator.store
        return json.loads(store.get_range(store.recorded(checkpoint_id)))
