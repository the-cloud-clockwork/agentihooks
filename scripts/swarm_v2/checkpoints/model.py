"""Checkpoint manifests: completeness and continuation decided from granted durable artifact storage alone."""

import json
from collections import Counter
from dataclasses import dataclass

from scripts.swarm_v2 import contracts
from scripts.swarm_v2.artifacts.base import CORRUPT, VERIFIED, ArtifactRef, ArtifactStore

METRIC = "checkpoint_completeness_by_reason"
CONTRACT = "checkpoint"
TYPED_MINOR = 2
COMPLETE = "complete"
INCOMPLETE = "incomplete"
DIAGNOSTIC = "diagnostic_only"
NATIVE = "native"
CONTEXT = "context"
NONE = "none"
TRANSCRIPT = "normalized_transcript"
NATIVE_SESSION = "native_session"
SOURCE_BUNDLE = "source_bundle"
TYPED = ("continuation", "branch_state", "inventory", "reconstruction")
CONSISTENT = frozenset({"harness_snapshot", "stopped_process", "consistent_export"})
SNAPSHOT_KEYS = frozenset({"worktrees", "conversation_id"})
VERSION_REFUSALS = frozenset({"missing_version", "unsupported_major", "unsupported_minor"})
PATH_ONLY_LIMITS = (
    "a saved worktree path is not a saved checkout",
    "a conversation id is not a saved conversation",
)
LEGACY_LIMITS = (
    "material sizes are unrecorded",
    "reconstruction metadata is unrecorded",
    "continuation is undeclared",
)
CONTEXT_LIMITS = ("native resume is not supported",)


@dataclass(frozen=True)
class Verdict:
    completeness: str
    continuation: str
    reason: str
    limitations: tuple[str, ...] = ()
    authoritative: bool = False


def _refused(reason: str) -> Verdict:
    return Verdict(INCOMPLETE, NONE, reason)


def _minor(doc: dict) -> int:
    return int(doc["schema_version"].split(".")[1])


def _counted(value: object) -> bool:
    return type(value) is int and value >= 0


def _missing_role(doc: dict) -> bool:
    roles = {TRANSCRIPT}
    if doc["branch_state"] == "bundled":
        roles.add(SOURCE_BUNDLE)
    if doc["continuation"] == NATIVE:
        roles.add(NATIVE_SESSION)
    return not roles <= doc["inventory"].keys()


def _native_problem(doc: dict) -> str | None:
    if doc["native_resume_capability"] != "qualified":
        return "native_unqualified"
    if not doc.get("native_session_id") or not doc.get("native_harness_version"):
        return "native_unqualified"
    if doc["quiescence_method"] not in CONSISTENT:
        return "inconsistent_capture"
    return None


def _typed_problem(doc: dict) -> str | None:
    if any(name not in doc for name in TYPED):
        return "missing_material"
    authority = doc["authority"]
    if doc["reconstruction"]["execution_generation"] != authority.get(
        "execution_generation", authority["task_generation"]
    ):
        return "scope_mismatch"
    cursor = doc["transcript_durable_cursor"]
    if not (_counted(cursor.get("sequence")) and _counted(cursor.get("byte_offset"))):
        return "missing_watermark"
    if _missing_role(doc):
        return "missing_material"
    if doc["continuation"] == NATIVE and (problem := _native_problem(doc)):
        return problem
    hashes = {role: "sha256:" + ref["sha256"] for role, ref in doc["inventory"].items()}
    return "inventory_mismatch" if doc["artifact_hashes"] != hashes else None


class Validator:
    """Every read goes through the store granted to this attempt; a verdict never names record values or paths."""

    def __init__(
        self, store: ArtifactStore, loaded: dict | None = None, execution_generation: int | None = None
    ) -> None:
        self.store = store
        self.execution_generation = execution_generation
        self.contracts = contracts.load() if loaded is None else loaded
        self.decisions: Counter[str] = Counter()

    def metrics(self) -> dict[str, dict[str, int]]:
        return {METRIC: dict(self.decisions)}

    def classify(self, doc: object) -> Verdict:
        return self._count(self._classify(doc))

    def verify(self, checkpoint_id: str) -> Verdict:
        return self._count(self._verify(checkpoint_id))

    def commit(self, doc: object) -> Verdict:
        return self._count(self._commit(doc))

    def _count(self, verdict: Verdict) -> Verdict:
        self.decisions[f"{verdict.completeness}/{verdict.reason}"] += 1
        return verdict

    def _classify(self, doc: object) -> Verdict:
        if isinstance(doc, dict) and "schema_version" not in doc and SNAPSHOT_KEYS & doc.keys():
            return Verdict(DIAGNOSTIC, NONE, "path_only", PATH_ONLY_LIMITS)
        refusal = contracts.check(self.contracts, CONTRACT, doc)
        if refusal:
            return _refused("unsupported_version" if refusal["reason"] in VERSION_REFUSALS else "invalid_record")
        if _minor(doc) < TYPED_MINOR:
            return Verdict(DIAGNOSTIC, NONE, "legacy_minor", LEGACY_LIMITS)
        if doc["status"] != "complete" or doc["committed_at"] is None:
            return _refused("not_committed")
        if problem := _typed_problem(doc):
            return _refused(problem)
        if (
            doc["authority"]["execution_id"] == self.store.scope.execution_id
            and "execution_generation" in doc["authority"]
            and self.execution_generation is not None
            and doc["authority"].get("execution_generation", doc["authority"]["task_generation"])
            != self.execution_generation
        ):
            return _refused("scope_mismatch")
        if doc["continuation"] == NATIVE:
            return Verdict(COMPLETE, NATIVE, NATIVE)
        return Verdict(COMPLETE, CONTEXT, CONTEXT, CONTEXT_LIMITS)

    def _objects(self, doc: dict) -> str | None:
        states = {self.store.stat(ArtifactRef(ref["sha256"], ref["size"])) for ref in doc["inventory"].values()}
        if states - {VERIFIED}:
            return "corrupt_object" if CORRUPT in states else "missing_object"
        return None

    def _commit(self, doc: object) -> Verdict:
        verdict = self._classify(doc)
        if verdict.completeness != COMPLETE:
            return verdict
        if self.contracts["compat"]["contracts"][CONTRACT]["write_minor"] < TYPED_MINOR:
            return _refused("promotion_disabled")
        scope, authority = self.store.scope, doc["authority"]
        current = (scope.task_id, scope.execution_id, scope.generation)
        if (authority["task_id"], authority["execution_id"], authority["task_generation"]) != current:
            return _refused("scope_mismatch")
        if problem := self._objects(doc):
            return _refused(problem)
        data = json.dumps(doc, sort_keys=True).encode()
        recorded = self.store.recorded(doc["checkpoint_id"])
        if recorded not in (None, ArtifactRef.of(data)):
            return _refused("identity_conflict")
        self.store.put(doc["checkpoint_id"], data)
        return self._verify(doc["checkpoint_id"])

    def _load(self, checkpoint_id: str) -> object:
        ref = self.store.recorded(checkpoint_id)
        if ref is None:
            return "manifest_absent"
        if self.store.stat(ref) != VERIFIED:
            return "corrupt_manifest"
        try:
            return json.loads(self.store.get_range(ref))
        except ValueError:
            return "corrupt_manifest"

    def _verify(self, checkpoint_id: str) -> Verdict:
        doc = self._load(checkpoint_id)
        if isinstance(doc, str):
            return _refused(doc)
        verdict = self._classify(doc)
        if verdict.completeness != COMPLETE:
            return verdict
        if doc["checkpoint_id"] != checkpoint_id:
            return _refused("identity_mismatch")
        authority = doc["authority"]
        if (
            authority["task_id"] != self.store.scope.task_id
            or authority["task_generation"] > self.store.scope.generation
        ):
            return _refused("scope_mismatch")
        if problem := self._objects(doc):
            return _refused(problem)
        return Verdict(verdict.completeness, verdict.continuation, verdict.reason, verdict.limitations, True)
