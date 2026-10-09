from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Protocol

OWNER_LABEL = "swarm.agentihooks.io/controller-owner"
EXECUTION_LABEL = "swarm.agentihooks.io/execution-id"
BACKEND = "kubernetes"
CLASSES = ("managed_orphan", "missing_pod", "foreign", "ambiguous", "terminating", "superseded")


class CursorExpired(Exception):
    pass


@dataclass(frozen=True)
class Pod:
    name: str
    uid: str
    labels: dict = field(default_factory=dict)
    deleting: bool = False


class PodSource(Protocol):
    def list_pods(self, selector: str) -> tuple[list[Pod], str]: ...

    def watch_pods(self, selector: str, resource_version: str) -> Iterator[tuple[str, Pod, str]]: ...

    def read_pod(self, name: str) -> Pod | None: ...

    def delete_pod(self, name: str, uid: str) -> None:
        """Delete with a uid precondition, so a recreated Pod of the same name survives."""
        ...


def owner_for(slug: str) -> str:
    return f"agentihooks-swarm-{slug}"


def labels(owner: str, execution_id: str) -> dict:
    return {OWNER_LABEL: owner, EXECUTION_LABEL: execution_id}


class PodView:
    def __init__(self, source: PodSource) -> None:
        self.source, self.selector = source, OWNER_LABEL
        self.resource_version, self.by_uid = "", {}

    def relist(self) -> None:
        pods, self.resource_version = self.source.list_pods(self.selector)
        self.by_uid = {pod.uid: pod for pod in pods}

    def sync(self) -> "PodView":
        if not self.resource_version:
            self.relist()
            return self
        try:
            for kind, pod, version in self.source.watch_pods(self.selector, self.resource_version):
                if kind == "DELETED":
                    self.by_uid.pop(pod.uid, None)
                else:
                    self.by_uid[pod.uid] = pod
                self.resource_version = version
        except (CursorExpired, ConnectionError, TimeoutError):
            self.relist()
        return self

    def pods(self) -> list[Pod]:
        return list(self.by_uid.values())


@dataclass
class Plan:
    matched: dict = field(default_factory=dict)
    missing_pods: list = field(default_factory=list)
    delete: list = field(default_factory=list)
    quarantine: list = field(default_factory=list)
    observed: Counter = field(default_factory=Counter)

    def counts(self) -> dict:
        return {name: self.observed[name] for name in CLASSES}


class Reconciler:
    def __init__(self, owner: str, cleanup: bool) -> None:
        self.owner, self.cleanup = owner, cleanup

    def plan(self, journals: Iterable[str], pods: Iterable[Pod], superseded: Iterable[str] = ()) -> Plan:
        journals, superseded, plan, by_execution = set(journals), set(superseded), Plan(), {}
        for pod in pods:
            execution_id = pod.labels.get(EXECUTION_LABEL, "")
            if pod.labels.get(OWNER_LABEL) != self.owner:
                plan.observed["foreign"] += 1
                plan.quarantine.append(pod)
            elif not execution_id:
                plan.observed["ambiguous"] += 1
                plan.quarantine.append(pod)
            else:
                by_execution.setdefault(execution_id, []).append(pod)
        for execution_id, group in sorted(by_execution.items()):
            self._settle(plan, journals, superseded, execution_id, group)
        plan.missing_pods = sorted(journals - set(by_execution))
        plan.observed["missing_pod"] = len(plan.missing_pods)
        return plan

    def _settle(self, plan: Plan, journals: set, superseded: set, execution_id: str, group: list[Pod]) -> None:
        live = [pod for pod in group if not pod.deleting]
        plan.observed["terminating"] += len(group) - len(live)
        if len(live) > 1:
            plan.observed["ambiguous"] += len(live)
            plan.quarantine.extend(live)
        elif live and execution_id in journals:
            plan.matched[execution_id] = live[0].uid
        elif live and execution_id in superseded:
            plan.observed["superseded"] += 1
        elif live:
            plan.observed["managed_orphan"] += 1
            if self.cleanup:
                plan.delete.append(live[0])
