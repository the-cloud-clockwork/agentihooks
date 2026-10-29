from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import asdict, replace
from typing import TYPE_CHECKING
from uuid import uuid4

from scripts.inbox.seats import is_seat
from scripts.swarm import fencing

IMMUTABLE = ("execution_id", "generation", "name", "lane", "task", "seat", "started_at", "runtime_backend", "transport")
EXECUTION_FIELDS = ("execution_id", "generation", "runtime_backend", "runtime_target")
PROJECTION_OMITTED = (*EXECUTION_FIELDS, "hive", "transport", "remote_control", "desired_route_kind")
WRITES: dict[str, threading.RLock] = {}


def _writes(slug: str) -> threading.RLock:
    return WRITES.setdefault(slug, threading.RLock())


if TYPE_CHECKING:
    from scripts.swarm.store import AgentRecord, RedisStore
    from scripts.swarm.tick import Runtime


def _record(row: dict) -> AgentRecord:
    from scripts.swarm.store import AgentRecord

    return AgentRecord(**row)


def latest(rows: Iterable[dict], by: str) -> dict:
    found: dict = {}
    for row in rows:
        previous = found.get(row[by])
        if previous is None or row["generation"] > previous["generation"]:
            found[row[by]] = row
        elif by == "seat" and row["generation"] == previous["generation"]:
            found[row[by]] = row
    return found


def _archivable(row: dict) -> bool:
    return (
        isinstance(row.get("name"), str)
        and isinstance(row.get("seat"), str)
        and isinstance(row.get("generation"), int | float)
    )


class ExecutionRegistry:
    def __init__(self, store: RedisStore) -> None:
        self.store = store

    def conflict(self, slug: str, message: str) -> None:
        from scripts.swarm.store import SwarmError

        fencing.bump(self.store, slug, fencing.IDENTITY_CONFLICTS)
        raise SwarmError(message)

    def _doc(self, slug: str, doc: dict | None) -> dict:
        return doc if doc is not None else fencing.read(self.store, slug)

    def records(self, slug: str, doc: dict | None = None) -> list[AgentRecord]:
        doc = self._doc(slug, doc)
        rows = {}
        for section in ("executions", "archive"):
            for row in doc[section].values():
                rows.setdefault(row["execution_id"], _record(row))
        return sorted(rows.values(), key=lambda a: (a.seat, a.generation))

    def occupants(self, slug: str, doc: dict | None = None) -> dict[str, AgentRecord]:
        heads = latest(self._doc(slug, doc)["executions"].values(), "seat")
        return {seat: _record(heads[seat]) for seat in sorted(heads)}

    def managed(self, slug: str, name: str, doc: dict | None = None) -> bool:
        doc = self._doc(slug, doc)
        return any(row["name"] == name for section in ("executions", "archive") for row in doc[section].values())

    def archive(self, slug: str) -> int:
        listed = self.store.listed(slug)

        def move(doc):
            rows = {key: row for key, row in doc["executions"].items() if _archivable(row)}
            heads = latest(rows.values(), "seat")
            old = [
                key
                for key, row in rows.items()
                if row["generation"] < heads[row["seat"]]["generation"] and row["name"] not in listed
            ]
            for key in old:
                doc["archive"][key] = doc["executions"].pop(key)
            return len(old)

        return fencing.write(self.store, slug, move)

    def hive(self, slug: str, name: str) -> str:
        if name not in self.store.listed(slug):
            return ""
        rows = [row for row in fencing.read(self.store, slug)["executions"].values() if row["name"] == name]
        hive = max(rows, key=lambda row: row["generation"]).get("hive", "") if rows else ""
        return hive if isinstance(hive, str) else ""

    def agents(self, slug: str) -> list[AgentRecord]:
        projections, desired = self.store.projections(slug)
        attempts = [row for row in fencing.read(self.store, slug)["executions"].values() if row["name"] in projections]
        heads = latest(attempts, "name")
        result = []
        for name, row in sorted(projections.items()):
            if name in heads:
                row.update({field: heads[name][field] for field in EXECUTION_FIELDS})
                row["hive"] = heads[name].get("hive", "")
                row["transport"] = heads[name].get("transport", "")
                row["remote_control"] = heads[name].get("remote_control", {})
            row["desired_route_kind"] = desired.get(name) or heads.get(name, {}).get("desired_route_kind", "")
            result.append(_record(row))
        return result

    def put(self, slug: str, agent: AgentRecord) -> None:
        with _writes(slug):
            if _has_identity(agent) or not self.store.project_legacy(
                slug, agent, lambda: self.managed(slug, agent.name)
            ):
                self._update(slug, agent)

    def get(self, slug: str, execution_id: str) -> AgentRecord:
        doc = fencing.read(self.store, slug)
        row = doc["executions"].get(execution_id) or doc["archive"].get(execution_id)
        if not row:
            self.conflict(slug, "unknown execution identity; display labels cannot identify attempts")
        return _record(row)

    def validate(self, slug: str, agent: AgentRecord) -> None:
        if agent.runtime_backend not in ("local", "kubernetes"):
            self.conflict(slug, "unsupported runtime backend")
        if not is_seat(agent.seat) or not agent.seat.endswith(f"@{slug}"):
            self.conflict(slug, "execution seat does not belong to the swarm")
        _validate_target(agent.runtime_backend, agent.runtime_target, lambda message: self.conflict(slug, message))

    def start(self, slug: str, agent: AgentRecord, previous_execution_id: str) -> AgentRecord:
        with _writes(slug):
            return self._start(slug, agent, previous_execution_id)

    def _start(self, slug: str, agent: AgentRecord, previous_execution_id: str) -> AgentRecord:
        from scripts.swarm import lease
        from scripts.swarm.store import SwarmError

        agent = replace(agent, name=self.store.names.resolve(agent.name))
        if not self.store.names.entry(agent.name) or self.store.names.slug_of(agent.name) != slug:
            self.conflict(slug, "execution requires a registered canonical agent name")
        if agent.execution_id or agent.generation:
            self.conflict(slug, "new execution identity and generation must be allocated by the store")
        self.validate(slug, agent)
        if self.store.publication(slug, agent.seat):
            raise SwarmError("current execution publication is in flight; retry after it finishes")
        epoch = lease.EPOCH.get()

        def admit(doc):
            previous = self.occupants(slug, doc).get(agent.seat)
            if (previous.execution_id if previous else "") != previous_execution_id:
                self.conflict(slug, "replacement does not match the current execution")
            rows = self.records(slug, doc)
            if any(row.name == agent.name and row.seat != agent.seat for row in rows):
                self.conflict(slug, "canonical agent already belongs to another execution seat")
            self.check_uid(slug, agent, doc)
            current = replace(
                agent, execution_id=f"exe-{uuid4().hex}", generation=previous.generation + 1 if previous else 1
            )
            if current.execution_id in doc["executions"]:
                self.conflict(slug, "execution identity already exists")
            doc["executions"][current.execution_id] = asdict(current)
            if epoch is not None:
                doc["intents"][current.execution_id] = {
                    "execution_id": current.execution_id,
                    "generation": current.generation,
                    "controller_epoch": epoch,
                }
            return current, previous

        current, previous = lease.fenced(self.store, slug, epoch, admit)
        removed = previous.name if previous and previous.name != current.name else ""
        self.store.project_agent(slug, current, removed=removed)
        return current

    def check_uid(self, slug: str, agent: AgentRecord, doc: dict | None = None) -> None:
        uid = agent.runtime_target.get("pod_uid")
        if uid and any(
            row.execution_id != agent.execution_id and row.runtime_target.get("pod_uid") == uid
            for row in self.records(slug, doc)
        ):
            self.conflict(slug, "Pod UID already belongs to another execution")

    def update(self, slug: str, agent: AgentRecord) -> None:
        with _writes(slug):
            self._update(slug, agent)

    def _update(self, slug: str, agent: AgentRecord) -> None:
        self.validate(slug, agent)

        def change(doc):
            current = self.occupants(slug, doc).get(agent.seat)
            if not current or any(getattr(agent, field) != getattr(current, field) for field in IMMUTABLE):
                self.conflict(slug, "stale or changed execution identity")
            _check_binding(current.runtime_target, agent.runtime_target, lambda message: self.conflict(slug, message))
            self.check_uid(slug, agent, doc)
            if agent != current:
                doc["executions"][agent.execution_id] = asdict(agent)
            return current

        current = fencing.write(self.store, slug, change)
        if agent != current:
            self.store.project_agent(slug, agent, current)

    def set_pane(self, slug: str, execution_id: str, pane_id: str) -> None:
        with _writes(slug):
            self._set_pane(slug, execution_id, pane_id)

    def _set_pane(self, slug: str, execution_id: str, pane_id: str) -> None:
        listed = self.store.listed(slug)

        def change(doc):
            raw = doc["executions"].get(execution_id) or doc["archive"].pop(execution_id, None)
            if not raw:
                self.conflict(slug, "unknown execution identity; display labels cannot identify attempts")
            current = _record(raw)
            heads = {head.execution_id for head in self.occupants(slug, doc).values()}
            updated = replace(current, pane_id=pane_id)
            doc["executions"][execution_id] = asdict(updated)
            return (updated, current) if execution_id in heads and current.name in listed else None

        shown = fencing.write(self.store, slug, change)
        if shown is not None:
            self.store.project_agent(slug, *shown, listed_only=True)

    def reconcile(self, slug: str, runtime: Runtime, now_ms: int) -> list[str]:
        self.archive(slug)
        recorded = {agent.name for agent in self.agents(slug)}
        live, actions = runtime.live_names(), []
        for agent in self.occupants(slug).values():
            if not _orphan_state(agent, now_ms) or agent.name in recorded or agent.name in live:
                continue
            if _process_gone(self.store, agent, runtime) and self.end_orphan(slug, agent, now_ms):
                actions.append(f"ended orphan execution {agent.execution_id} for {agent.name}")
        return actions

    def end_orphan(self, slug: str, agent: AgentRecord, now_ms: int) -> bool:
        listed = self.store.listed(slug)
        ended = asdict(replace(agent, state="ended"))

        def end(doc):
            current = self.occupants(slug, doc).get(agent.seat)
            if current != agent or not _orphan_state(agent, now_ms) or agent.name in listed:
                return False
            doc["executions"][agent.execution_id] = ended
            return True

        def undo(doc):
            if doc["executions"].get(agent.execution_id) == ended:
                doc["executions"][agent.execution_id] = asdict(agent)

        if not fencing.write(self.store, slug, end):
            return False
        if agent.name not in self.store.listed(slug):
            return True
        fencing.write(self.store, slug, undo)
        return False


def _validate_target(backend, target, refuse):
    if not isinstance(target, dict):
        refuse("runtime target must be a structured identity")
    allowed = (
        {"pod_namespace", "pod_name", "pod_uid"}
        if backend == "kubernetes"
        else {"host_id", "server_id", "process_namespace", "pid", "pid_start"}
    )
    if set(target) - allowed:
        refuse("unsupported runtime target identity field")
    if backend == "kubernetes" and not all(
        isinstance(target.get(field), str) and target[field] for field in ("pod_namespace", "pod_name")
    ):
        refuse("Kubernetes target requires namespace and Pod name")
    for field, value in target.items():
        if field == "pid":
            if type(value) is not int or value < 1:
                refuse("runtime PID must be a positive integer")
        elif field == "pid_start":
            if type(value) is not int or value < 1:
                refuse("runtime PID start time must be a positive integer")
        elif not isinstance(value, str):
            refuse("runtime target identity must be a string")


def _check_binding(previous, current, refuse):
    if set(previous) == {"host_id"} and set(current) == {"host_id", "process_namespace", "pid", "pid_start"}:
        if previous["host_id"] == current["host_id"]:
            return
    for field in sorted(set(previous) | set(current)):
        if field == "pod_uid" and not previous.get(field):
            continue
        if previous.get(field) != current.get(field):
            refuse("runtime target identity is immutable after binding")


def _has_identity(agent):
    return bool(
        agent.execution_id or agent.generation or agent.runtime_backend != "local" or agent.runtime_target or agent.hive
    )


def _orphan_state(agent: AgentRecord, now_ms: int) -> bool:
    from scripts.swarm.master_start import DEADLINE_MS

    if agent.state == "starting":
        return now_ms - agent.started_at >= DEADLINE_MS
    return agent.state == "working"


def _process_gone(store: RedisStore, agent: AgentRecord, runtime: Runtime) -> bool:
    from scripts.hive import reports

    if runtime.reported(agent):
        return False
    remote = reports.host(agent)
    if remote:
        report = store.hive_report(remote)
        return report is not None and agent.name not in report["names"]
    if agent.runtime_backend != "local":
        return runtime.observe(agent).state == "dead"
    return True
