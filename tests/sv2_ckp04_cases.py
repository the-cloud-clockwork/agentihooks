import hashlib
import json
from pathlib import Path

from scripts.swarm_v2 import contracts
from scripts.swarm_v2.artifacts import local
from scripts.swarm_v2.artifacts.base import VERIFIED, ArtifactRef
from scripts.swarm_v2.checkpoints import model, publisher
from scripts.swarm_v2.worker import supervisor
from tests import sv2_ckp02_cases as ckp02

EVIDENCE_CLASS = (
    "mocked: a local durable directory backend behind a granted artifact store, fixture launch registrations, a "
    "backend wrapper that loses acknowledgements and a fake clock; no object store, worker Pod or harness was used"
)


class LostAck:
    """Performs every mutating call, then reports a failure for the one numbered lose_at."""

    kind = "local"

    def __init__(self, root: Path, lose_at: int | None = None) -> None:
        self.inner = local.LocalBackend(root)
        self.lose_at = lose_at
        self.calls: list[tuple[str, str]] = []

    def _mutated(self, name: str, key: str) -> None:
        self.calls.append((name, key))
        if len(self.calls) == self.lose_at:
            raise ConnectionError(f"acknowledgement lost after {name} {key}")

    def write(self, key: str, data: bytes) -> None:
        self.inner.write(key, data)
        self._mutated("write", key)

    def move(self, source: str, target: str) -> None:
        self.inner.move(source, target)
        self._mutated("move", target)

    def remove(self, key: str) -> None:
        self.inner.remove(key)

    def size(self, key: str) -> int | None:
        return self.inner.size(key)

    def read(self, key: str, start: int, length: int) -> bytes:
        return self.inner.read(key, start, length)

    def keys(self, prefix: str) -> list[str]:
        return self.inner.keys(prefix)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def checkpoint(sequence: int, generation: int = 2, execution: str = "e1", payload: bytes | None = None):
    doc = ckp02.manifest_for(f"ckpt-g{generation}-s{sequence}")
    doc["checkpoint_sequence"] = sequence
    doc["authority"] = {**doc["authority"], "execution_id": execution, "task_generation": generation}
    doc["reconstruction"] = {**doc["reconstruction"], "execution_generation": generation}
    data = payload if payload is not None else json.dumps({"generation": generation, "sequence": sequence}).encode()
    return doc, {model.TRANSCRIPT: data}


def make(root: Path, execution: str = "e1", generation: int = 2, backend=None) -> publisher.Publisher:
    return publisher.Publisher(model.Validator(ckp02.store(root, execution, generation, backend)))


def files(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def pointers(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*.json") if "/pointers/" in path.as_posix())


def digest(root: Path) -> str:
    return hashlib.sha256(json.dumps({key: value.hex() for key, value in files(root).items()}).encode()).hexdigest()


def shown(result: publisher.Publication) -> dict:
    verdict = None if result.verdict is None else ckp02.shown(result.verdict)
    return {"promoted": result.promoted, "reason": result.reason, "verdict": verdict}


def _referenced(pub: publisher.Publisher, root: Path) -> dict:
    store = pub.validator.store
    states = {}
    for name in pointers(root):
        pointer = json.loads((root / name).read_text())
        manifest = ArtifactRef(**pointer["manifest"])
        doc = json.loads(store.get_range(manifest))
        refs = [manifest, *(ArtifactRef(**ref) for ref in doc["inventory"].values())]
        states[pointer["checkpoint_id"]] = sorted({store.stat(ref) for ref in refs})
    return states


def _positive_fixture(root: Path) -> dict:
    clock = Clock()
    pub = make(root)
    sched = supervisor.Schedule(pub, lambda sequence: checkpoint(sequence), supervisor.Budget(interval=300), clock)
    before = sched.metrics()
    first = sched.tick()
    clock.now += 120
    early = sched.tick()
    clock.now += 180
    second = sched.tick()
    clock.now += 45
    reader = make(root, "e2", 3)
    return {
        "metrics_before_first_publish": before,
        "first": shown(first),
        "tick_before_interval": early,
        "second": shown(second),
        "pointers": pointers(root),
        "referenced_object_states": _referenced(pub, root),
        "latest_for_writer": pub.latest(),
        "latest_for_next_attempt": reader.latest(),
        "next_attempt_verdict": ckp02.shown(reader.validator.verify(reader.latest())),
        "metrics": sched.metrics(),
    }


def _positive() -> tuple[dict, bool]:
    with ckp02.scratch() as (one, two):
        observed = {"fixture": _positive_fixture(one), "second_fixture": _positive_fixture(two)}
    first = observed["fixture"]
    checks = [
        observed["fixture"] == observed["second_fixture"],
        first["first"]["promoted"] and first["second"]["promoted"],
        first["tick_before_interval"] is None,
        all(states == [VERIFIED] for states in first["referenced_object_states"].values()),
        first["latest_for_writer"] == first["latest_for_next_attempt"] == "ckpt-g2-s2",
        first["next_attempt_verdict"]["authoritative"],
        first["metrics"] == {"checkpoint_age_seconds": 45.0, "checkpoint_publish_failures": {}},
    ]
    return observed, all(checks)


def _rejection_fixture(root: Path) -> dict:
    stale = make(root, "e1", 2)
    stale.publish(*checkpoint(1))
    newer = make(root, "e2", 3)
    newer.publish(*checkpoint(1, 3, "e2"))
    protected = digest(root)
    superseded = stale.publish(*checkpoint(2))
    after_superseded = digest(root)
    conflicting = newer.publish(*checkpoint(1, 3, "e2", b"other"))
    doc, materials = checkpoint(1, 3, "e2", b"other")
    doc["checkpoint_id"] = doc["operation_id"] = "ckpt-other"
    stale_sequence = newer.publish(doc, materials)
    outside = newer.publish(*checkpoint(2, 3, "e9"))
    after_refusals = digest(root)
    racing = make(root / "race", "e1", 2)
    commit = racing.validator.commit

    def interleaved(doc):
        make(root / "race", "e2", 3).publish(*checkpoint(1, 3, "e2"))
        return commit(doc)

    racing.validator.commit = interleaved
    raced = racing.publish(*checkpoint(1))
    restoring = make(root, "e3", 4).latest()
    corrected = newer.publish(*checkpoint(2, 3, "e2"))
    return {
        "superseded": shown(superseded),
        "superseded_left_store_unchanged": after_superseded == protected,
        "same_identity_other_content": shown(conflicting),
        "stale_sequence": shown(stale_sequence),
        "outside_grant": shown(outside),
        "refusals_left_store_unchanged": after_refusals == protected,
        "raced_during_upload": shown(raced),
        "raced_pointers": pointers(root / "race"),
        "latest_for_restoring_attempt": restoring,
        "latest_for_stale_attempt": stale.latest(),
        "stale_metrics": stale.metrics(),
        "newer_metrics": newer.metrics(),
        "corrected_request": shown(corrected),
    }


def _rejection() -> tuple[dict, bool]:
    with ckp02.scratch() as (one, two):
        observed = {"fixture": _rejection_fixture(one), "second_fixture": _rejection_fixture(two)}
    first = observed["fixture"]
    checks = [
        observed["fixture"] == observed["second_fixture"],
        first["superseded"]["reason"] == publisher.SUPERSEDED,
        first["superseded_left_store_unchanged"],
        first["same_identity_other_content"]["reason"] == publisher.CONFLICT,
        first["stale_sequence"]["reason"] == publisher.STALE,
        first["outside_grant"]["reason"] == publisher.SCOPE,
        first["refusals_left_store_unchanged"],
        first["raced_during_upload"]["reason"] == publisher.SUPERSEDED,
        first["raced_pointers"] == ["s1/t1/pointers/0000000003/0000000001.json"],
        first["latest_for_restoring_attempt"] == "ckpt-g3-s1",
        first["latest_for_stale_attempt"] == "",
        first["corrected_request"]["promoted"],
    ]
    return observed, all(checks)


def _boundary(key: str) -> str:
    parts = key.split("/")
    return "/".join(parts[2:4]) if parts[2] in ("artifacts", "pointers") else parts[2]


def _lost(root: Path, lose_at: int) -> dict:
    backend = LostAck(root)
    pub = make(root, backend=backend)
    pub.publish(*checkpoint(1))
    backend.calls.clear()
    backend.lose_at = lose_at
    try:
        pub.publish(*checkpoint(2))
        raised = False
    except ConnectionError:
        raised = True
    name, key = backend.calls[lose_at - 1]
    current = pub.latest()
    authoritative = pub.validator.verify(current).authoritative
    backend.lose_at = None
    replay = pub.publish(*checkpoint(2))
    return {
        "boundary": f"{name} {_boundary(key)}",
        "raised": raised,
        "latest_after_loss": current,
        "latest_authoritative": authoritative,
        "retry": shown(replay),
        "latest_after_retry": pub.latest(),
        "pointers_after_retry": len(pointers(root)),
    }


def _recovery_fixture(root: Path) -> dict:
    count = LostAck(root / "count")
    counted = make(root / "count", backend=count)
    counted.publish(*checkpoint(1))
    count.calls.clear()
    counted.publish(*checkpoint(2))
    boundaries = [_lost(root / f"lose-{index}", index) for index in range(1, len(count.calls) + 1)]
    crashed = make(root / "crash")
    crashed.publish(*checkpoint(1, payload=b"first"))
    commit = crashed.validator.commit

    def stopped(doc):
        raise ConnectionError("stopped before the manifest commit")

    crashed.validator.commit = stopped
    try:
        crashed.publish(*checkpoint(2, payload=b"second"))
    except ConnectionError:
        pass
    crashed.validator.commit = commit
    store = crashed.validator.store
    orphan = store.recorded("ckpt-g2-s2.normalized_transcript")
    return {
        "boundaries": boundaries,
        "crash_manifest_recorded": store.recorded("ckpt-g2-s2") is not None,
        "crash_latest": crashed.latest(),
        "crash_old_checkpoint": ckp02.shown(crashed.validator.verify("ckpt-g2-s1")),
        "orphan_state_before_reclaim": store.stat(orphan),
        "reclaim_committed": crashed.reclaim("ckpt-g2-s1"),
        "reclaimed": crashed.reclaim("ckpt-g2-s2"),
        "orphan_state_after_reclaim": store.stat(orphan),
        "old_object_state_after_reclaim": store.stat(ArtifactRef.of(b"first")),
        "old_checkpoint_after_reclaim": ckp02.shown(crashed.validator.verify("ckpt-g2-s1")),
        "republished": shown(crashed.publish(*checkpoint(2, payload=b"second"))),
        "latest_after_republish": crashed.latest(),
        **_rollback(crashed),
    }


def _rollback(pub: publisher.Publisher) -> dict:
    loaded = contracts.load()
    compat = loaded["compat"]["contracts"]
    lowered = {**compat, "checkpoint": {**compat["checkpoint"], "write_minor": 1}}
    rolled = publisher.Publisher(
        model.Validator(pub.store, {**loaded, "compat": {**loaded["compat"], "contracts": lowered}})
    )
    return {
        "rollback_publish": shown(rolled.publish(*checkpoint(3))),
        "latest_after_rollback": rolled.latest(),
        "restored_publish": shown(pub.publish(*checkpoint(3))),
        "latest_after_restore": pub.latest(),
    }


def _recovery() -> tuple[dict, bool]:
    with ckp02.scratch() as (one, two):
        observed = {"fixture": _recovery_fixture(one), "second_fixture": _recovery_fixture(two)}
    first = observed["fixture"]
    last = first["boundaries"][-1]
    checks = [
        observed["fixture"] == observed["second_fixture"],
        all(entry["raised"] and entry["latest_authoritative"] for entry in first["boundaries"]),
        all(entry["latest_after_loss"] == "ckpt-g2-s1" for entry in first["boundaries"][:-1]),
        last["boundary"] == "write pointers/0000000002" and last["latest_after_loss"] == "ckpt-g2-s2",
        all(
            entry["retry"]["promoted"] and entry["latest_after_retry"] == "ckpt-g2-s2" for entry in first["boundaries"]
        ),
        all(entry["pointers_after_retry"] == 2 for entry in first["boundaries"]),
        last["retry"]["reason"] == publisher.REPLAYED,
        not first["crash_manifest_recorded"],
        first["crash_latest"] == "ckpt-g2-s1",
        first["crash_old_checkpoint"]["authoritative"],
        first["reclaim_committed"] == [],
        first["reclaimed"] == ["ckpt-g2-s2.normalized_transcript"],
        first["orphan_state_after_reclaim"] == "absent",
        first["old_object_state_after_reclaim"] == VERIFIED,
        first["old_checkpoint_after_reclaim"]["authoritative"],
        first["latest_after_republish"] == "ckpt-g2-s2",
        first["rollback_publish"]["reason"] == "promotion_disabled",
        first["latest_after_rollback"] == "ckpt-g2-s2",
        first["restored_publish"]["promoted"],
        first["latest_after_restore"] == "ckpt-g2-s3",
    ]
    return observed, all(checks)


def run_case(case: str) -> dict:
    observed, passed = {"a": _positive, "b": _rejection, "c": _recovery}[case]()
    return {
        "case": f"T-SV2-CKP-04-{case.upper()}",
        "state": "passed" if passed else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {ckp02.TRANSCRIPT.name: hashlib.sha256(ckp02.TRANSCRIPT.read_bytes()).hexdigest()},
        "observed": json.loads(json.dumps(observed)),
    }
