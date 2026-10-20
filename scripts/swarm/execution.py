from __future__ import annotations

import json
import time
from dataclasses import asdict, replace
from typing import TYPE_CHECKING
from uuid import uuid4

from scripts.inbox.seats import is_seat

IMMUTABLE = ("execution_id", "generation", "name", "lane", "task", "seat", "started_at", "runtime_backend", "transport")
WRITE_ATTEMPTS = 5
EXECUTION_FIELDS = ("execution_id", "generation", "runtime_backend", "runtime_target")
PROJECTION_OMITTED = (*EXECUTION_FIELDS, "hive", "transport", "remote_control", "desired_route_kind")


if TYPE_CHECKING:
    from redis import Redis
    from redis.client import Pipeline

    from scripts.swarm.store import AgentRecord, RedisStore
    from scripts.swarm.tick import Runtime


ADMIT = """
local raw = redis.call('GET', KEYS[4])
if not raw then return 'controller_stale' end
local leader = cjson.decode(raw)
local expected = cjson.decode(ARGV[3])
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
if leader.owner ~= expected.owner or leader.epoch ~= expected.epoch or leader.expires_at <= now then
    return 'controller_stale'
end
local agent = cjson.decode(ARGV[1])
redis.call('HSET', KEYS[1], agent.execution_id, ARGV[1])
redis.call('HSET', KEYS[2], agent.name, ARGV[2])
redis.call('HSET', KEYS[3], agent.name, ARGV[4])
redis.call('HSET', KEYS[5], agent.execution_id, ARGV[6])
if ARGV[5] ~= '' then redis.call('HDEL', KEYS[2], ARGV[5]) end
return 'committed'
"""


class ExecutionRegistry:
    def __init__(self, store: RedisStore) -> None:
        self.store = store
        self.redis = store.redis

    def conflict(self, slug: str, message: str) -> None:
        from scripts.swarm.store import SwarmError

        self.redis.incr(self.store.key(slug, "identity-conflicts"))
        raise SwarmError(message)

    def records(self, slug: str, reader: Redis | Pipeline | None = None) -> list[AgentRecord]:
        from scripts.swarm.store import AgentRecord

        rows = (reader if reader is not None else self.redis).hvals(self.store.key(slug, "executions"))
        return sorted((AgentRecord(**json.loads(row)) for row in rows), key=lambda a: (a.seat, a.generation))

    def occupants(self, slug: str, reader: Redis | Pipeline | None = None) -> dict[str, AgentRecord]:
        return {agent.seat: agent for agent in self.records(slug, reader)}

    def managed(self, slug: str, name: str, reader: Redis | Pipeline | None = None) -> bool:
        return any(agent.name == name for agent in self.records(slug, reader))

    def agents(self, slug: str) -> list[AgentRecord]:
        from scripts.swarm.store import AgentRecord

        with self.redis.pipeline() as pipe:
            pipe.hgetall(self.store.key(slug, "agents"))
            pipe.hvals(self.store.key(slug, "executions"))
            pipe.hgetall(self.store.key(slug, "desired-routes"))
            projections, attempts, desired = pipe.execute()
        latest = {}
        for raw in attempts:
            row = json.loads(raw)
            if row["name"] not in latest or row["generation"] > latest[row["name"]]["generation"]:
                latest[row["name"]] = row
        result = []
        for name, raw in sorted(projections.items()):
            row = json.loads(raw)
            if name in latest:
                row.update({field: latest[name][field] for field in EXECUTION_FIELDS})
                row["hive"] = latest[name].get("hive", "")
                row["transport"] = latest[name].get("transport", "")
                row["remote_control"] = latest[name].get("remote_control", {})
            row["desired_route_kind"] = desired.get(name) or latest.get(name, {}).get("desired_route_kind", "")
            result.append(AgentRecord(**row))
        return result

    def put(self, slug: str, agent: AgentRecord) -> None:
        from redis.exceptions import WatchError

        from scripts.swarm.store import SwarmError

        for _ in range(WRITE_ATTEMPTS):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(self.store.key(slug, "executions"), self.store.key(slug, "agents"))
                    if _has_identity(agent) or self.managed(slug, agent.name, pipe):
                        pipe.unwatch()
                        self.update(slug, agent)
                        return
                    previous = pipe.hget(self.store.key(slug, "agents"), agent.name)
                    pipe.multi()
                    _write_projection(pipe, self.store, slug, agent)
                    self.write_status(pipe, slug, agent, previous)
                    pipe.execute()
                    return
                except WatchError:
                    continue
        raise SwarmError("agent registry kept changing; update was not committed")

    def write_status(self, pipe: Pipeline, slug: str, agent: AgentRecord, previous: str | None) -> None:
        from scripts.swarm.store import AgentRecord
        from scripts.swarm.tick import agent_status

        if not previous or agent_status(AgentRecord(**json.loads(previous))) != agent_status(agent):
            pipe.hset(self.store.key(slug, "state-since"), agent.name, int(time.time() * 1000))

    def get(self, slug: str, execution_id: str) -> AgentRecord:
        from scripts.swarm.store import AgentRecord

        raw = self.redis.hget(self.store.key(slug, "executions"), execution_id)
        if not raw:
            self.conflict(slug, "unknown execution identity; display labels cannot identify attempts")
        return AgentRecord(**json.loads(raw))

    def validate(self, slug: str, agent: AgentRecord) -> None:
        if agent.runtime_backend not in ("local", "kubernetes"):
            self.conflict(slug, "unsupported runtime backend")
        if not is_seat(agent.seat) or not agent.seat.endswith(f"@{slug}"):
            self.conflict(slug, "execution seat does not belong to the swarm")
        _validate_target(agent.runtime_backend, agent.runtime_target, lambda message: self.conflict(slug, message))

    def start(self, slug: str, agent: AgentRecord, previous_execution_id: str) -> AgentRecord:
        from redis.exceptions import WatchError

        from scripts.swarm.store import SwarmError

        agent = replace(agent, name=self.store.names.resolve(agent.name))
        if not self.store.names.entry(agent.name) or self.store.names.slug_of(agent.name) != slug:
            self.conflict(slug, "execution requires a registered canonical agent name")
        if agent.execution_id or agent.generation:
            self.conflict(slug, "new execution identity and generation must be allocated by the store")
        self.validate(slug, agent)
        from scripts.codex_session.binding import PUBLISHING

        key = self.store.key(slug, "executions")
        publishing = self.store.key(slug, PUBLISHING)
        for _ in range(WRITE_ATTEMPTS):
            with self.redis.pipeline() as pipe:
                try:
                    from scripts.swarm import lease

                    pipe.watch(key, publishing)
                    if pipe.hget(publishing, agent.seat):
                        raise SwarmError("current execution publication is in flight; retry after it finishes")
                    epoch = lease.EPOCH.get()
                    held = lease.current(self.store, slug) if epoch is not None else None
                    if epoch is not None and (held is None or held.epoch != epoch):
                        raise SwarmError("the controller lease is stale")
                    previous = self.occupants(slug, pipe).get(agent.seat)
                    if (previous.execution_id if previous else "") != previous_execution_id:
                        self.conflict(slug, "replacement does not match the current execution")
                    rows = self.records(slug, pipe)
                    if any(row.name == agent.name and row.seat != agent.seat for row in rows):
                        self.conflict(slug, "canonical agent already belongs to another execution seat")
                    self.check_uid(slug, agent, pipe)
                    current = replace(
                        agent, execution_id=f"exe-{uuid4().hex}", generation=previous.generation + 1 if previous else 1
                    )
                    if pipe.hexists(key, current.execution_id):
                        self.conflict(slug, "execution identity already exists")
                    self._admit(pipe, slug, current, previous, held)
                    return current
                except WatchError:
                    continue
        raise SwarmError("execution history kept changing; admission was not committed")

    def _admit(self, pipe, slug, current, previous, held) -> None:
        from scripts.swarm.store import SwarmError

        removed = previous.name if previous and previous.name != current.name else ""
        pipe.multi()
        if held is None:
            self.write(pipe, slug, current)
            if removed:
                pipe.hdel(self.store.key(slug, "agents"), removed)
            pipe.execute()
            return
        row = asdict(current)
        intent = {
            "execution_id": current.execution_id,
            "generation": current.generation,
            "controller_epoch": held.epoch,
        }
        keys = [
            self.store.key(slug, part)
            for part in ("executions", "agents", "state-since", "control-owner", "controller-intents")
        ]
        pipe.eval(
            ADMIT,
            len(keys),
            *keys,
            json.dumps(row),
            json.dumps({field: value for field, value in row.items() if field not in PROJECTION_OMITTED}),
            json.dumps(asdict(held)),
            int(time.time() * 1000),
            removed,
            json.dumps(intent),
        )
        if pipe.execute()[0] != "committed":
            raise SwarmError("the controller lease is stale")

    def check_uid(self, slug: str, agent: AgentRecord, reader: Redis | Pipeline) -> None:
        uid = agent.runtime_target.get("pod_uid")
        if uid and any(
            row.execution_id != agent.execution_id and row.runtime_target.get("pod_uid") == uid
            for row in self.records(slug, reader)
        ):
            self.conflict(slug, "Pod UID already belongs to another execution")

    def update(self, slug: str, agent: AgentRecord) -> None:
        from redis.exceptions import WatchError

        from scripts.swarm.store import SwarmError

        self.validate(slug, agent)
        key = self.store.key(slug, "executions")
        for _ in range(WRITE_ATTEMPTS):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    current = self.occupants(slug, pipe).get(agent.seat)
                    if not current or any(getattr(agent, field) != getattr(current, field) for field in IMMUTABLE):
                        self.conflict(slug, "stale or changed execution identity")
                    _check_binding(
                        current.runtime_target, agent.runtime_target, lambda message: self.conflict(slug, message)
                    )
                    self.check_uid(slug, agent, pipe)
                    if agent == current:
                        pipe.unwatch()
                        return
                    pipe.multi()
                    self.write(pipe, slug, agent, current)
                    pipe.execute()
                    return
                except WatchError:
                    continue
        raise SwarmError("execution history kept changing; update was not committed")

    def set_pane(self, slug: str, execution_id: str, pane_id: str) -> None:
        from redis.exceptions import WatchError

        from scripts.swarm.store import AgentRecord, SwarmError

        key = self.store.key(slug, "executions")
        for _ in range(WRITE_ATTEMPTS):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    raw = pipe.hget(key, execution_id)
                    if not raw:
                        pipe.unwatch()
                        self.conflict(slug, "unknown execution identity; display labels cannot identify attempts")
                    current = AgentRecord(**json.loads(raw))
                    pipe.multi()
                    self.write(pipe, slug, replace(current, pane_id=pane_id), current)
                    pipe.execute()
                    return
                except WatchError:
                    continue
        raise SwarmError("execution history kept changing; pane was not committed")

    def reconcile(self, slug: str, runtime: Runtime) -> list[str]:
        recorded = {agent.name for agent in self.agents(slug)}
        live, actions = runtime.live_names(), []
        for agent in self.occupants(slug).values():
            if agent.state != "working" or agent.name in recorded or agent.name in live:
                continue
            if _process_gone(self.redis, agent, runtime) and self.end_orphan(slug, agent):
                actions.append(f"ended orphan execution {agent.execution_id} for {agent.name}")
        return actions

    def end_orphan(self, slug: str, agent: AgentRecord) -> bool:
        from redis.exceptions import WatchError

        key = self.store.key(slug, "executions")
        with self.redis.pipeline() as pipe:
            try:
                pipe.watch(key, self.store.key(slug, "agents"))
                current = self.occupants(slug, pipe).get(agent.seat)
                if (
                    current != agent
                    or agent.state != "working"
                    or pipe.hexists(self.store.key(slug, "agents"), agent.name)
                ):
                    return False
                pipe.multi()
                pipe.hset(key, agent.execution_id, json.dumps(asdict(replace(agent, state="ended"))))
                pipe.execute()
                return True
            except WatchError:
                return False

    def write(self, pipe: Pipeline, slug: str, agent: AgentRecord, previous: AgentRecord | None = None) -> None:
        from scripts.swarm.tick import agent_status

        raw = json.dumps(asdict(agent))
        pipe.hset(self.store.key(slug, "executions"), agent.execution_id, raw)
        _write_projection(pipe, self.store, slug, agent)
        if previous is None or agent_status(previous) != agent_status(agent):
            pipe.hset(self.store.key(slug, "state-since"), agent.name, int(time.time() * 1000))


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


def _process_gone(redis: Redis, agent: AgentRecord, runtime: Runtime) -> bool:
    from scripts.hive import reports

    if runtime.reported(agent):
        return False
    remote = reports.host(agent)
    if remote:
        report = reports.Reports(redis).read(remote)
        return report is not None and agent.name not in report["names"]
    if agent.runtime_backend != "local":
        return runtime.observe(agent).state == "dead"
    return True


def _write_projection(pipe, store, slug, agent):
    pipe.hset(
        store.key(slug, "agents"),
        agent.name,
        json.dumps({field: value for field, value in asdict(agent).items() if field not in PROJECTION_OMITTED}),
    )
    if agent.desired_route_kind:
        pipe.hset(store.key(slug, "desired-routes"), agent.name, agent.desired_route_kind)
    else:
        pipe.hdel(store.key(slug, "desired-routes"), agent.name)
