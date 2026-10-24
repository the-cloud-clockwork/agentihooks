"""The number doctor measure prints for one finding id or one finding class, refusing any it cannot stand behind."""

from dataclasses import dataclass

from scripts.doctor import traces
from scripts.swarm.health.findings import Finding
from scripts.swarm.store import SwarmError

LAG_CLASSES = ("telemetry-stale", "exporter-backlog")


@dataclass(frozen=True)
class Read:
    count: int
    unknown: int
    maximum: int

    def line(self, name: str) -> str:
        return f"{name} count {self.count} unknown {self.unknown} max {self.maximum}"


def class_of(kind: str) -> str:
    return kind.replace(" ", "-")


def _agents(evidence):
    return [e[len("agent ") :] for e in evidence if e.startswith("agent ")]


def _class_hint(finding_id):
    return f"measure the class {finding_id.partition('/')[0]} to read every live target"


def _unknown(target, failed):
    return SwarmError(f"{target} is unknown because {'; '.join(failed)}")


def exact(finding_id: str, found: list[Finding], failed: list[str], seen: dict, live: set[str]) -> int:
    """A current finding's number; 0 only for a seen finding that is gone while the agent it names is live."""
    match = next((f for f in found if f.id == finding_id), None)
    if match:
        return match.measure
    if failed:
        raise _unknown(finding_id, failed)
    record = seen.get(finding_id)
    if record is None:
        raise SwarmError(f"no finding {finding_id} has been seen; {_class_hint(finding_id)}")
    gone = [agent for agent in _agents(record["evidence"]) if agent not in live]
    if gone:
        raise SwarmError(f"{finding_id} names agent {gone[0]}, which is no longer live; {_class_hint(finding_id)}")
    return 0


def spawn_count(finding_id: str, found: list[Finding], tasks: set[str]) -> int:
    """Failed spawns of a ledger task, or the master, in the journal window; 0 is a reading only for a known one."""
    subject = finding_id.partition("/")[2]
    if subject != "master" and subject not in tasks:
        raise SwarmError(f"{finding_id} names no task on the watched ledger")
    return next((f.measure for f in found if f.id == finding_id), 0)


def _measures(name, found):
    return [f.measure for f in found if class_of(f.kind) == name]


def of_class(name: str, found: list[Finding], failed: list[str], seen_classes: set[str]) -> Read:
    """Every current finding of a class seen before; unknown counts the detectors that failed."""
    measures = _measures(name, found)
    if not measures and failed:
        raise _unknown(name, failed)
    if not measures and name not in seen_classes:
        raise SwarmError(f"no finding of the class {name} has been seen")
    return Read(len(measures), len(failed), max(measures, default=0))


def lag(local: dict | None, now_ms: int) -> int | None:
    return None if local is None else traces.unaccepted_seconds({"local": local}, now_ms)


def of_lag_class(name: str, found: list[Finding], lags: list[int | None]) -> Read:
    """Every active binding: unknown when its exporter progress is unreadable, the maximum over its unaccepted
    seconds and the class's current findings."""
    known = [seconds for seconds in lags if seconds is not None]
    return Read(len(lags), len(lags) - len(known), max([*known, *_measures(name, found)], default=0))
