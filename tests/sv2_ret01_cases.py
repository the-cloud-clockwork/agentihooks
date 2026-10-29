import json
from dataclasses import dataclass, field
from pathlib import Path

from hooks.memory.retention import Policies
from scripts.swarm_v2.checkpoints import retention

FIXTURE = Path(__file__).parent / "fixtures" / "sv2_ret01" / "fixture.json"
DAY = 86400
FIXTURE_INPUTS = {
    "expired_unreferenced_checkpoint": "ckpt-old of finished task t-done, past the swarm native resume age",
    "active_restore": "ckpt-restore of active task t-restore under a registered restore hold",
    "held_task_evidence": "evidence-held, raw swarm evidence past its age under an accepted evidence hold",
    "personal_retention_policy": "personal-retention-1, keeping raw evidence and vectors and expiring previews",
}


class Interrupted(OSError):
    pass


@dataclass
class Inventory:
    stored: dict[str, retention.StoredObject]
    manifests: list[retention.Checkpoint]
    removals: list[str] = field(default_factory=list)
    fail_on: int = 0

    def objects(self) -> list[retention.StoredObject]:
        return list(self.stored.values())

    def checkpoints(self) -> list[retention.Checkpoint]:
        return [item for item in self.manifests if item.checkpoint_id in self.stored]

    def remove(self, object_id: str) -> None:
        if self.fail_on and len(self.removals) + 1 == self.fail_on:
            self.fail_on = 0
            raise Interrupted("transport lost before the removal")
        del self.stored[object_id]
        self.removals.append(object_id)


@dataclass
class Holds:
    restores: set[str]
    held: set[str]
    active: set[str]

    def restoring(self) -> frozenset[str]:
        return frozenset(self.restores)

    def evidence(self) -> frozenset[str]:
        return frozenset(self.held)

    def active_tasks(self) -> frozenset[str]:
        return frozenset(self.active)


def fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def world(doc: dict | None = None) -> tuple[Inventory, Holds, Policies, float]:
    doc = fixture() if doc is None else doc
    now = float(doc["now"])
    stored = {}
    for item in doc["objects"]:
        extra = {"retention": item["retention"]} if "retention" in item else {}
        created = now - item["age_days"] * DAY
        stored[item["object_id"]] = retention.StoredObject(
            item["object_id"], item["corpus"], item["data_class"], created, **extra
        )
    manifests = [
        retention.Checkpoint(c["checkpoint_id"], c["task_id"], c["sequence"], c["complete"], tuple(c["objects"]))
        for c in doc["checkpoints"]
    ]
    holds = doc["holds"]
    return (
        Inventory(stored, manifests),
        Holds(set(holds["restoring"]), set(holds["evidence"]), set(holds["active_tasks"])),
        Policies.parse(doc["policies"]),
        now,
    )


def collector(root: Path, inventory, holds, policies, now, destructive=True) -> retention.Collector:
    journal = retention.FileJournal(root / "deletion-journal.jsonl")
    return retention.Collector(inventory, holds, policies, journal, lambda: now, destructive)


def _ops(root: Path, run_id: str) -> list[list[str]]:
    entries = retention.FileJournal(root / "deletion-journal.jsonl").entries(run_id)
    return [[entry["op"], entry.get("object_id", ""), entry.get("reason", "")] for entry in entries]


def positive(root: Path) -> dict:
    runs = []
    for index in (1, 2):
        folder = root / f"fixture-{index}"
        folder.mkdir()
        inventory, holds, policies, now = world()
        report = collector(folder, inventory, holds, policies, now).collect("run-a")
        replay = collector(folder, inventory, holds, policies, now).collect("run-a")
        runs.append(
            {
                "decisions": report.decisions,
                "deleted": list(report.deleted),
                "remaining": sorted(inventory.stored),
                "removals": inventory.removals,
                "replay_deleted": list(replay.deleted),
                "metrics": report.metrics(),
                "journal": _ops(folder, "run-a"),
            }
        )
    doc = fixture()
    doc["policies"] = [policy for policy in doc["policies"] if policy["corpus"] == "swarm"]
    inventory, holds, policies, now = world(doc)
    undeclared = collector(root, inventory, holds, policies, now).plan()
    return {
        "case": "A",
        "first": runs[0],
        "independent_fixture_matches": runs[0] == runs[1],
        "personal_without_policy": {k: v for k, v in undeclared.decisions.items() if k.startswith("personal-")},
    }


def rejection(root: Path) -> dict:
    inventory, holds, policies, now = world()
    before = sorted(inventory.stored)
    refusals = {}
    for object_id in ("ckpt-restore", "restore-native", "evidence-held", "ckpt-live-2"):
        try:
            collector(root, inventory, holds, policies, now).delete(object_id)
        except retention.RetentionRefused as error:
            refusals[object_id] = str(error)
    unchanged = sorted(inventory.stored) == before and inventory.removals == []
    dry = collector(root, inventory, holds, policies, now, destructive=False).collect("run-b")
    after_dry_run = {"removals": list(inventory.removals), "journal": _ops(root, "run-b"), "eligible": dry.eligible()}
    holds.restores.add("ckpt-old")
    resumed = collector(root, inventory, holds, policies, now).collect("run-b")
    return {
        "case": "B",
        "refusals": refusals,
        "state_unchanged_after_refusals": unchanged,
        "rollback_dry_run": after_dry_run,
        "restore_started_mid_run": {
            "deleted": list(resumed.deleted),
            "skipped": resumed.skipped,
            "restore_objects_present": all(o in inventory.stored for o in ("ckpt-old", "old-native", "old-transcript")),
            "journal": _ops(root, "run-b"),
        },
    }


def recovery(root: Path) -> dict:
    inventory, holds, policies, now = world()
    inventory.fail_on = 2
    interrupted = ""
    try:
        collector(root, inventory, holds, policies, now).collect("run-c")
    except Interrupted as error:
        interrupted = str(error)
    survived = {"removals": list(inventory.removals), "journal": _ops(root, "run-c")}
    holds.held.add("log-old")
    resumed = collector(root, inventory, holds, policies, now).collect("run-c")
    replay = collector(root, inventory, holds, policies, now).collect("run-c")
    return {
        "case": "C",
        "interrupted": interrupted,
        "after_interruption": survived,
        "resumed": {"deleted": list(resumed.deleted), "skipped": resumed.skipped},
        "removals": inventory.removals,
        "duplicate_removals": len(inventory.removals) - len(set(inventory.removals)),
        "replay_removals": len(inventory.removals),
        "newly_protected_present": "log-old" in inventory.stored,
        "journal": _ops(root, "run-c"),
        "metrics": replay.metrics(),
    }


CASES = {"A": positive, "B": rejection, "C": recovery}


def run_case(case: str, root: Path) -> dict:
    return CASES[case](root)
