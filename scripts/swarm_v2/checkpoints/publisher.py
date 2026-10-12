"""Checkpoint publication: materials, then a verified manifest, then a write-once pointer under the task generation."""

import json
from collections import Counter
from dataclasses import asdict, dataclass

from scripts.swarm_v2.artifacts.base import ArtifactRef, ArtifactStore
from scripts.swarm_v2.checkpoints import model, native, redaction

METRIC = "checkpoint_publish_failures"
PROMOTED = "promoted"
REPLAYED = "replayed"
SUPERSEDED = "superseded_generation"
STALE = "stale_sequence"
SCOPE = "scope_mismatch"
CONFLICT = "identity_conflict"
INVALID = "invalid_sequence"
WIDTH = 10


@dataclass(frozen=True)
class Publication:
    """A superseded_generation refusal may follow this attempt's own pointer write; latest never reads that pointer."""

    promoted: bool
    reason: str
    verdict: model.Verdict | None = None


@dataclass(frozen=True)
class Pointer:
    generation: int
    sequence: int
    document: dict


def _listed(doc: dict, materials: dict[str, bytes]) -> dict:
    refs = {role: asdict(ArtifactRef.of(data)) for role, data in materials.items()}
    hashes = {role: "sha256:" + ref["sha256"] for role, ref in refs.items()}
    return {
        **doc,
        "inventory": {**doc.get("inventory", {}), **refs},
        "artifact_hashes": {**doc.get("artifact_hashes", {}), **hashes},
    }


def _encoded(doc: dict) -> bytes:
    return json.dumps(doc, sort_keys=True).encode()


def _read(store: ArtifactStore, key: str) -> dict:
    return json.loads(store.backend.read(key, 0, store.backend.size(key)))


def _scrubbed(material: bytes) -> tuple[bytes, int]:
    cleaned = {name: redaction.scrub(data) for name, data in native.members(material).items()}
    files = tuple((name, data) for name, (data, _) in cleaned.items())
    if native.oversized(files):
        raise native.NativeError(native.TOO_LARGE)
    return native.archive(files), sum(count for _, count in cleaned.values())


def _withheld(doc: dict, materials: dict[str, bytes]) -> tuple[dict, dict[str, bytes]]:
    """Native files are redacted before any upload; the manifest keeps only the count."""
    if model.NATIVE_SESSION not in materials:
        return doc, materials
    scrubbed, count = _scrubbed(materials[model.NATIVE_SESSION])
    total = doc.get(redaction.FIELD, 0) + count
    return {**doc, redaction.FIELD: total}, {**materials, model.NATIVE_SESSION: scrubbed}


class Publisher:
    """Only a pointer makes a checkpoint current. The generation's single owner writes it once its manifest verifies;
    a newer generation's pointer fences an older attempt before its manifest commit, before and after its pointer
    write, and the latest lookup reads the newest generation alone."""

    def __init__(self, validator: model.Validator) -> None:
        self.validator = validator
        self.failures: Counter[str] = Counter()

    @property
    def store(self) -> ArtifactStore:
        return self.validator.store

    def metrics(self) -> dict[str, dict[str, int]]:
        return {METRIC: dict(self.failures)}

    def refuse(self, reason: str, verdict: model.Verdict | None = None) -> Publication:
        self.failures[reason] += 1
        return Publication(False, reason, verdict)

    def publish(self, doc: dict, materials: dict[str, bytes]) -> Publication:
        scope, authority = self.store.scope, doc["authority"]
        claimed = (authority["task_id"], authority["execution_id"], authority["task_generation"])
        if claimed != (scope.task_id, scope.execution_id, scope.generation):
            return self.refuse(SCOPE)
        checkpoint_id, sequence = doc["checkpoint_id"], doc["checkpoint_sequence"]
        if type(sequence) is not int or sequence < 0:
            return self.refuse(INVALID)
        blocked = self._blocked(checkpoint_id, sequence)
        if blocked and blocked != REPLAYED:
            return self.refuse(blocked)
        try:
            doc, materials = _withheld(doc, materials)
        except (native.NativeError, redaction.Unredactable) as error:
            return self.refuse(str(error))
        listed = _listed(doc, materials)
        recorded = self.store.recorded(checkpoint_id)
        if recorded not in (None, ArtifactRef.of(_encoded(listed))):
            return self.refuse(CONFLICT)
        if blocked:
            return self._replayed(checkpoint_id)
        if recorded is None:
            self.reclaim(checkpoint_id)
        return self._promote(listed, materials)

    def _replayed(self, checkpoint_id: str) -> Publication:
        verdict = self.validator.verify(checkpoint_id)
        if not verdict.authoritative:
            return self.refuse(verdict.reason, verdict)
        return Publication(True, REPLAYED, verdict)

    def _promote(self, doc: dict, materials: dict[str, bytes]) -> Publication:
        checkpoint_id = doc["checkpoint_id"]
        for role, data in materials.items():
            self.store.put(f"{checkpoint_id}.{role}", data)
        if self._superseded(self._pointers()):
            return self.refuse(SUPERSEDED)
        verdict = self.validator.commit(doc)
        if not verdict.authoritative:
            return self.refuse(verdict.reason, verdict)
        if self._superseded(self._pointers()):
            return self.refuse(SUPERSEDED, verdict)
        self._point(checkpoint_id, doc["checkpoint_sequence"])
        if self._superseded(self._pointers()):
            return self.refuse(SUPERSEDED, verdict)
        return Publication(True, PROMOTED, verdict)

    def latest(self) -> str:
        found = self._pointers()
        if not found:
            return ""
        newest = max(pointer.generation for pointer in found)
        candidates = [pointer for pointer in found if pointer.generation == newest]
        for pointer in sorted(candidates, key=lambda pointer: pointer.sequence, reverse=True):
            if self._current(pointer.document):
                return pointer.document["checkpoint_id"]
        return ""

    def sequence(self) -> int:
        own = [pointer.sequence for pointer in self._pointers() if pointer.generation == self.store.scope.generation]
        return max(own, default=0)

    def reclaim(self, checkpoint_id: str) -> list[str]:
        """Removes the records of a checkpoint no pointer names, never a newer generation's record; an object another
        record names stays."""
        if checkpoint_id in {pointer.document["checkpoint_id"] for pointer in self._pointers()}:
            return []
        scope, backend = self.store.scope, self.store.backend
        prefix = scope.key("artifacts") + "/"
        records = {
            key.removeprefix(prefix).removesuffix(".json"): _read(self.store, key) for key in backend.keys(prefix)
        }
        owned = sorted(
            name
            for name, record in records.items()
            if (name == checkpoint_id or name.startswith(checkpoint_id + "."))
            and record["generation"] <= scope.generation
        )
        kept = {record["sha256"] for name, record in records.items() if name not in owned}
        for name in owned:
            backend.remove(f"{prefix}{name}.json")
        for digest in sorted({records[name]["sha256"] for name in owned} - kept):
            backend.remove(scope.key("objects", digest))
        return owned

    def _blocked(self, checkpoint_id: str, sequence: int) -> str | None:
        scope = self.store.scope
        found = self._pointers()
        if self._superseded(found):
            return SUPERSEDED
        own = [pointer for pointer in found if pointer.generation == scope.generation]
        if any(pointer.document["execution_id"] != scope.execution_id for pointer in own):
            return SCOPE
        if any(pointer.document["checkpoint_id"] == checkpoint_id and pointer.sequence == sequence for pointer in own):
            return REPLAYED
        if any(pointer.sequence >= sequence for pointer in own):
            return STALE
        return None

    def _superseded(self, found: list[Pointer]) -> bool:
        return any(pointer.generation > self.store.scope.generation for pointer in found)

    def _pointers(self) -> list[Pointer]:
        found = []
        for key in self.store.backend.keys(self.store.scope.key("pointers") + "/"):
            generation, sequence = key.removesuffix(".json").split("/")[-2:]
            found.append(Pointer(int(generation), int(sequence), _read(self.store, key)))
        return found

    def _current(self, document: dict) -> bool:
        checkpoint_id = document["checkpoint_id"]
        if self.store.recorded(checkpoint_id) != ArtifactRef(**document["manifest"]):
            return False
        return self.validator.verify(checkpoint_id).authoritative

    def _point(self, checkpoint_id: str, sequence: int) -> None:
        scope = self.store.scope
        document = {
            "checkpoint_id": checkpoint_id,
            "checkpoint_sequence": sequence,
            "execution_id": scope.execution_id,
            "generation": scope.generation,
            "manifest": asdict(self.store.recorded(checkpoint_id)),
        }
        key = scope.key("pointers", f"{scope.generation:0{WIDTH}d}", f"{sequence:0{WIDTH}d}.json")
        self.store.backend.write(key, json.dumps(document).encode())
