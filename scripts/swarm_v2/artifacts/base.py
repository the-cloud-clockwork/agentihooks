"""One artifact contract over any durable backend: content counts as stored only when its read back matches."""

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from typing import NoReturn, Protocol

from scripts.swarm_v2.auth_context import IDENTIFIER, Registration

METRIC = "artifact_upload_verification_failures"
UNGRANTED = (
    "an artifact store takes its scope only from the registration its authorization returns for a launch grant token"
)
CHUNK = 1 << 20
ABSENT = "absent"
CORRUPT = "corrupt"
VERIFIED = "verified"
DIGEST = re.compile(r"[0-9a-f]{64}")


class ArtifactError(ValueError):
    pass


def _refuse(message: str) -> NoReturn:
    raise ArtifactError(message)


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        _refuse(f"not an identifier: {value!r}")
    return value


def _encode(document: dict) -> bytes:
    return json.dumps(document, sort_keys=True).encode()


@dataclass(frozen=True)
class ArtifactRef:
    sha256: str
    size: int

    def __post_init__(self) -> None:
        if not DIGEST.fullmatch(self.sha256) or self.size < 0:
            _refuse(f"not a content reference: {self.sha256}:{self.size}")

    @classmethod
    def of(cls, data: bytes) -> "ArtifactRef":
        return cls(hashlib.sha256(data).hexdigest(), len(data))


@dataclass(frozen=True)
class Scope:
    swarm_id: str
    task_id: str
    execution_id: str
    generation: int

    def __post_init__(self) -> None:
        for value in (self.swarm_id, self.task_id, self.execution_id):
            _identifier(value)

    @classmethod
    def granted(cls, registration: Registration) -> "Scope":
        return cls(registration.swarm_id, registration.task_id, registration.execution_id, registration.generation)

    def key(self, *parts: str) -> str:
        return "/".join((self.swarm_id, self.task_id, *parts))


class Backend(Protocol):
    kind: str

    def write(self, key: str, data: bytes) -> None: ...

    def size(self, key: str) -> int | None: ...

    def read(self, key: str, start: int, length: int) -> bytes: ...

    def move(self, source: str, target: str) -> None: ...

    def remove(self, key: str) -> None: ...

    def keys(self, prefix: str) -> list[str]: ...


class ArtifactStore:
    """The authorization callback must validate a launch grant token with the launch authority; the scope comes from it
    once, at construction, and holds for the store's lifetime, so a caller builds one store per attempt."""

    def __init__(self, backend: Backend, authorize: Callable[[str], Registration], token: str) -> None:
        registration = authorize(token) if isinstance(token, str) and token else None
        if not isinstance(registration, Registration):
            _refuse(UNGRANTED)
        self.backend = backend
        self._scope = Scope.granted(registration)
        self.failures: Counter[str] = Counter()

    @property
    def scope(self) -> Scope:
        return self._scope

    def metrics(self) -> dict[str, dict[str, int]]:
        return {METRIC: dict(self.failures)}

    def put(self, artifact_id: str, data: bytes) -> ArtifactRef:
        ref = ArtifactRef.of(data)
        recorded = self.recorded(artifact_id)
        if recorded not in (None, ref):
            _refuse(f"artifact {artifact_id} is committed with other content")
        if self.stat(ref) != VERIFIED:
            self._publish(self.scope.key("objects", ref.sha256), data)
        if recorded is None:
            record = {**asdict(ref), "execution_id": self.scope.execution_id, "generation": self.scope.generation}
            self._publish(_record_key(self.scope, artifact_id), _encode(record))
        return ref

    def recorded(self, artifact_id: str) -> ArtifactRef | None:
        document = self._document(_record_key(self.scope, artifact_id))
        return None if document is None else _reference(document)

    def stat(self, ref: ArtifactRef) -> str:
        key = self.scope.key("objects", ref.sha256)
        if self.backend.size(key) is None:
            return ABSENT
        return VERIFIED if self._matches(key, ref) else CORRUPT

    def get_range(self, ref: ArtifactRef, start: int = 0, length: int | None = None) -> bytes:
        length = ref.size - start if length is None else length
        if start < 0 or length < 0 or start + length > ref.size:
            _refuse(f"range {start}+{length} is outside {ref.size} bytes")
        if self.stat(ref) != VERIFIED:
            _refuse(f"artifact {ref.sha256} is not verified in {self.backend.kind}")
        return self.backend.read(self.scope.key("objects", ref.sha256), start, length)

    def commit_manifest(self, name: str, artifact_ids: Iterable[str]) -> dict:
        _identifier(name)
        scope = self.scope
        artifacts = {}
        for artifact_id in sorted(set(artifact_ids)):
            ref = self.recorded(artifact_id)
            if ref is None or self.stat(ref) != VERIFIED:
                _refuse(f"artifact {artifact_id} is not committed and verified")
            artifacts[artifact_id] = asdict(ref)
        newest = max(self._generations(name), default=scope.generation)
        if newest > scope.generation:
            _refuse(f"manifest {name} has newer generation {newest}")
        manifest = {
            "artifacts": artifacts,
            "execution_id": scope.execution_id,
            "generation": scope.generation,
            "name": name,
        }
        key = scope.key("manifests", name, f"{scope.generation}.json")
        existing = self._document(key)
        if existing is None:
            self._publish(key, _encode(manifest))
        elif existing != manifest:
            _refuse(f"manifest {name} generation {scope.generation} is committed with other content")
        return manifest

    def delete_if_unreferenced(self, ref: ArtifactRef) -> bool:
        listed = asdict(ref)
        for key in self.backend.keys(self.scope.key("manifests") + "/"):
            if listed in self._document(key)["artifacts"].values():
                return False
        records = {key: self._document(key) for key in self.backend.keys(self.scope.key("artifacts") + "/")}
        owned = [key for key, document in records.items() if _reference(document) == ref]
        if any(records[key]["generation"] > self.scope.generation for key in owned):
            return False
        for key in owned:
            self.backend.remove(key)
        self.backend.remove(self.scope.key("objects", ref.sha256))
        return True

    def _generations(self, name: str) -> list[int]:
        keys = self.backend.keys(self.scope.key("manifests", name) + "/")
        return [int(key.rsplit("/", 1)[1].removesuffix(".json")) for key in keys]

    def _document(self, key: str) -> dict | None:
        size = self.backend.size(key)
        return None if size is None else json.loads(self.backend.read(key, 0, size))

    def _matches(self, key: str, ref: ArtifactRef) -> bool:
        if self.backend.size(key) != ref.size:
            return False
        digest = hashlib.sha256()
        for start in range(0, ref.size, CHUNK):
            digest.update(self.backend.read(key, start, min(CHUNK, ref.size - start)))
        return digest.hexdigest() == ref.sha256

    def _publish(self, key: str, data: bytes) -> None:
        ref = ArtifactRef.of(data)
        staging = self.scope.key("staging", self.scope.execution_id, ref.sha256)
        try:
            self.backend.write(staging, data)
            self._confirm(staging, ref)
            self.backend.move(staging, key)
        finally:
            self.backend.remove(staging)
        self._confirm(key, ref)

    def _confirm(self, key: str, ref: ArtifactRef) -> None:
        if self._matches(key, ref):
            return
        self.failures[self.backend.kind] += 1
        self.backend.remove(key)
        _refuse(
            f"{self.backend.kind} reported a successful write of {ref.size} bytes to {key} but the read back does not match {ref.sha256}"
        )


def _reference(document: dict) -> ArtifactRef:
    return ArtifactRef(document["sha256"], document["size"])


def _record_key(scope: Scope, artifact_id: str) -> str:
    return scope.key("artifacts", f"{_identifier(artifact_id)}.json")
