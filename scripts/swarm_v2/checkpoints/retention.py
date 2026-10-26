"""Checkpoint and corpus collection: the eligibility plan is journaled before any delete, every delete re-reads the
restore and evidence holds, and an interrupted run resumes from its journal under the holds current at resume."""

import json
import os
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from hooks.memory.retention import CLASSES, UNTIL_EXPIRY, Policies

METRIC = "retention_protected_objects"
SWITCH = "SWARM_RETENTION_DESTRUCTIVE"
ELIGIBLE = "eligible"
ACTIVE_RESTORE = "active_restore"
EVIDENCE_HOLD = "evidence_hold"
LATEST_CHECKPOINT = "latest_checkpoint"
REFERENCED = "referenced"
NO_POLICY = "no_policy"
NOT_EXPIRED = "not_expired"
UNKNOWN_CLASS = "unknown_class"
PLANNED, DELETED, SKIPPED, ABSENT = "planned", "deleted", "skipped", "absent"


class RetentionRefused(ValueError):
    pass


@dataclass(frozen=True)
class StoredObject:
    object_id: str
    corpus: str
    data_class: str
    created_at: float
    retention: str = UNTIL_EXPIRY


@dataclass(frozen=True)
class Checkpoint:
    """The manifest is itself a stored object whose id is the checkpoint id."""

    checkpoint_id: str
    task_id: str
    sequence: int
    complete: bool
    objects: tuple[str, ...] = ()


class Inventory(Protocol):
    def objects(self) -> list[StoredObject]: ...

    def checkpoints(self) -> list[Checkpoint]: ...

    def remove(self, object_id: str) -> None: ...


class Holds(Protocol):
    """A restore registers its checkpoint here before it reads the manifest and releases it after the last read."""

    def restoring(self) -> frozenset[str]: ...

    def evidence(self) -> frozenset[str]: ...

    def active_tasks(self) -> frozenset[str]: ...


class Journal(Protocol):
    def entries(self, run_id: str) -> list[dict]: ...

    def append(self, run_id: str, entry: dict) -> None: ...


class FileJournal:
    """Append only JSON lines, synced per entry; a torn final line from a crash is ignored and closed off."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def entries(self, run_id: str) -> list[dict]:
        if not self.path.exists():
            return []
        found = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("run_id") == run_id:
                found.append(entry)
        return found

    def append(self, run_id: str, entry: dict) -> None:
        torn = self.path.exists() and not self.path.read_bytes().endswith(b"\n") and self.path.stat().st_size > 0
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(("\n" if torn else "") + json.dumps({**entry, "run_id": run_id}, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def destructive(env: Mapping[str, str] = os.environ) -> bool:
    return env.get(SWITCH, "on") != "off"


def _latest(checkpoints: list[Checkpoint], active: frozenset[str]) -> set[str]:
    best: dict[str, Checkpoint] = {}
    for checkpoint in checkpoints:
        if checkpoint.complete and checkpoint.task_id in active:
            held = best.get(checkpoint.task_id)
            if held is None or checkpoint.sequence > held.sequence:
                best[checkpoint.task_id] = checkpoint
    return {checkpoint.checkpoint_id for checkpoint in best.values()}


def _pinned(checkpoints: list[Checkpoint], restoring: frozenset[str], latest: set[str]) -> dict[str, str]:
    pinned: dict[str, str] = {}
    for checkpoint in checkpoints:
        if checkpoint.checkpoint_id in restoring:
            reason = ACTIVE_RESTORE
        elif checkpoint.checkpoint_id in latest:
            reason = LATEST_CHECKPOINT
        else:
            continue
        for object_id in (checkpoint.checkpoint_id, *checkpoint.objects):
            if pinned.get(object_id) != ACTIVE_RESTORE:
                pinned[object_id] = reason
    return pinned


@dataclass(frozen=True)
class Report:
    run_id: str
    decisions: dict[str, str]
    destructive: bool
    deleted: tuple[str, ...] = ()
    skipped: dict[str, str] = field(default_factory=dict)

    def eligible(self) -> list[str]:
        return [object_id for object_id, reason in self.decisions.items() if reason == ELIGIBLE]

    def metrics(self) -> dict[str, dict[str, int]]:
        return {METRIC: dict(sorted(Counter(r for r in self.decisions.values() if r != ELIGIBLE).items()))}


class Collector:
    def __init__(
        self,
        inventory: Inventory,
        holds: Holds,
        policies: Policies,
        journal: Journal,
        clock: Callable[[], float],
        destructive: bool = True,
    ) -> None:
        self.inventory, self.holds, self.policies = inventory, holds, policies
        self.journal, self.clock, self.destructive = journal, clock, destructive

    def plan(self, run_id: str = "") -> Report:
        """A dry run: the eligibility of every stored object, deleting nothing and writing no journal entry."""
        return Report(run_id, self._decisions(), False)

    def delete(self, object_id: str) -> None:
        reason = self._decisions().get(object_id, ABSENT)
        if reason != ELIGIBLE:
            raise RetentionRefused(f"object {object_id} is not eligible for deletion: {reason}")
        if not self.destructive:
            raise RetentionRefused(f"destructive collection is off; object {object_id} is kept")
        self.inventory.remove(object_id)

    def collect(self, run_id: str) -> Report:
        entries = self.journal.entries(run_id)
        if not entries:
            report = self.plan(run_id)
            planned = _ordered(report.eligible(), self.inventory.checkpoints())
            self.journal.append(
                run_id,
                {
                    "op": PLANNED,
                    "eligible": planned,
                    "protected": report.metrics()[METRIC],
                    "policies": self.policies.versions(),
                    "at": self.clock(),
                },
            )
            entries = self.journal.entries(run_id)
        if not self.destructive:
            return Report(run_id, self.plan(run_id).decisions, False)
        return self._execute(run_id, entries)

    def _execute(self, run_id: str, entries: list[dict]) -> Report:
        planned = entries[0]["eligible"]
        settled = {entry["object_id"]: entry for entry in entries[1:]}
        decisions = self._decisions()
        by_id = {checkpoint.checkpoint_id: checkpoint for checkpoint in self.inventory.checkpoints()}
        for object_id in planned:
            if object_id in settled:
                continue
            reason = self._held_now(by_id).get(object_id) or decisions.get(object_id, ABSENT)
            if reason == ABSENT:
                self.journal.append(run_id, {"op": ABSENT, "object_id": object_id})
            elif reason != ELIGIBLE:
                self.journal.append(run_id, {"op": SKIPPED, "object_id": object_id, "reason": reason})
            else:
                self.inventory.remove(object_id)
                self.journal.append(run_id, {"op": DELETED, "object_id": object_id})
        final = self.journal.entries(run_id)[1:]
        deleted = tuple(entry["object_id"] for entry in final if entry["op"] == DELETED)
        skipped = {entry["object_id"]: entry["reason"] for entry in final if entry["op"] == SKIPPED}
        return Report(run_id, decisions, True, deleted, skipped)

    def _held_now(self, by_id: dict[str, Checkpoint]) -> dict[str, str]:
        held = {object_id: EVIDENCE_HOLD for object_id in self.holds.evidence()}
        for checkpoint_id in self.holds.restoring():
            checkpoint = by_id.get(checkpoint_id)
            for object_id in (checkpoint_id, *(checkpoint.objects if checkpoint else ())):
                held[object_id] = ACTIVE_RESTORE
        return held

    def _decisions(self) -> dict[str, str]:
        now, objects, checkpoints = self.clock(), self.inventory.objects(), self.inventory.checkpoints()
        pinned = _pinned(checkpoints, self.holds.restoring(), _latest(checkpoints, self.holds.active_tasks()))
        evidence = self.holds.evidence()
        decisions = {item.object_id: self._own(item, now, pinned, evidence) for item in objects}
        for checkpoint in checkpoints:
            if decisions.get(checkpoint.checkpoint_id, ELIGIBLE) == ELIGIBLE:
                continue
            for object_id in checkpoint.objects:
                if decisions.get(object_id) == ELIGIBLE:
                    decisions[object_id] = REFERENCED
        return dict(sorted(decisions.items()))

    def _own(self, item: StoredObject, now: float, pinned: dict[str, str], evidence: frozenset[str]) -> str:
        if item.object_id in pinned:
            return pinned[item.object_id]
        if item.object_id in evidence:
            return EVIDENCE_HOLD
        if item.data_class not in CLASSES:
            return UNKNOWN_CLASS
        policy = self.policies.get(item.corpus)
        if policy is None:
            return NO_POLICY
        if not policy.expired(item.data_class, item.created_at, now, item.retention):
            return NOT_EXPIRED
        return ELIGIBLE


def _ordered(eligible: list[str], checkpoints: list[Checkpoint]) -> list[str]:
    """Manifests go before the objects they name, so a crash never leaves a manifest pointing at a deleted object."""
    manifests = {checkpoint.checkpoint_id for checkpoint in checkpoints}
    return sorted(eligible, key=lambda object_id: (object_id not in manifests, object_id))
