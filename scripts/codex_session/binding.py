"""Versioned Codex transport side record, one per swarm execution, beside the strict execution registry."""

import json
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.swarm.store import RedisStore

SCHEMA_VERSION = 1
TRANSPORT = "codex_app_server"
STARTING, CONNECTED, FAILED, EXITED, DEGRADED = "starting", "connected", "failed", "exited", "degraded"
STOOD_DOWN = "stood_down"
RECORDS, ENDPOINTS, OPENINGS = "codex-transport", "codex-endpoints", "codex-openings"
VIEWERS = "codex-viewers"
PUBLISHING = "execution-publishing"
RESERVE_ATTEMPTS = 5
IDENTITY = ("execution_id", "execution_generation", "seat", "agent_name", "task_id", "endpoint_ref", "transport")


class BindingError(RuntimeError):
    pass


@dataclass(frozen=True)
class TransportRecord:
    execution_id: str
    execution_generation: int
    seat: str
    agent_name: str
    task_id: str
    endpoint_ref: str
    schema_version: int = SCHEMA_VERSION
    seat_generation: int = 0
    transport: str = TRANSPORT
    server_pid: int = 0
    server_start_identity: int = 0
    host_id: str = ""
    thread_id: str = ""
    session_id: str = ""
    active_turn_id: str = ""
    account: str = ""
    profile: str = ""
    bundle_revision: str = ""
    model: str = ""
    effort: str = ""
    bridge_owner: str = ""
    bridge_epoch: int = 0
    lease_expires_at: int = 0
    state: str = STARTING
    last_transport_activity: int = 0
    last_reconcile_at: int = 0
    last_error_class: str = ""
    pending_request: str = ""


def decode(raw: str) -> TransportRecord:
    row = json.loads(raw)
    if row.get("schema_version") != SCHEMA_VERSION:
        raise BindingError("unsupported transport record version")
    return TransportRecord(**row)


class BindingStore:
    def __init__(self, store: "RedisStore") -> None:
        self.store = store
        self.redis = store.redis

    def reserve(self, slug: str, record: TransportRecord) -> TransportRecord:
        from redis.exceptions import WatchError

        records, endpoints = self.store.key(slug, RECORDS), self.store.key(slug, ENDPOINTS)
        record = replace(record, state=STARTING)
        for _ in range(RESERVE_ATTEMPTS):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(records, endpoints)
                    if pipe.hexists(records, record.execution_id):
                        raise BindingError("duplicate launch: the execution already has a transport record")
                    if pipe.hget(endpoints, record.endpoint_ref):
                        raise BindingError("the endpoint already belongs to another execution")
                    pipe.multi()
                    pipe.hset(records, record.execution_id, json.dumps(asdict(record)))
                    pipe.hset(endpoints, record.endpoint_ref, record.execution_id)
                    pipe.execute()
                    return record
                except WatchError:
                    continue
        raise BindingError("a concurrent launch changed the transport records; this launch was refused")

    def get(self, slug: str, execution_id: str) -> TransportRecord | None:
        raw = self.redis.hget(self.store.key(slug, RECORDS), execution_id)
        return decode(raw) if raw else None

    def put(self, slug: str, record: TransportRecord) -> None:
        current = self.get(slug, record.execution_id)
        if current is None:
            raise BindingError("no transport record for this execution")
        if any(getattr(current, field) != getattr(record, field) for field in IDENTITY):
            raise BindingError("transport record identity is immutable")
        self.redis.hset(self.store.key(slug, RECORDS), record.execution_id, json.dumps(asdict(record)))

    def release(self, slug: str, record: TransportRecord) -> None:
        from scripts.inbox.receipts import transact

        endpoints = self.store.key(slug, ENDPOINTS)
        publishing = self.store.key(slug, PUBLISHING)

        def release(pipe):
            pipe.watch(endpoints, publishing, self.store.key(slug, RECORDS))
            endpoint = pipe.hget(endpoints, record.endpoint_ref)
            held = pipe.hget(publishing, record.seat)
            current = self.get(slug, record.execution_id)
            pipe.multi()
            if endpoint == record.execution_id:
                pipe.hdel(endpoints, record.endpoint_ref)
            if held == record.execution_id and current.state in {EXITED, FAILED}:
                pipe.hdel(publishing, record.seat)

        transact(self.redis, release)

    def claim_opening(self, slug: str, execution_id: str, at: int) -> bool:
        return bool(self.redis.hsetnx(self.store.key(slug, OPENINGS), execution_id, at))

    def add_viewer(self, slug: str, execution_id: str, pane_id: str, terminal_id: str) -> None:
        self.redis.hset(self.store.key(slug, VIEWERS), pane_id, json.dumps([execution_id, terminal_id]))

    def viewers(self, slug: str, execution_id: str) -> dict[str, str]:
        rows = {pane: json.loads(raw) for pane, raw in self.redis.hgetall(self.store.key(slug, VIEWERS)).items()}
        return {pane: terminal for pane, (owner, terminal) in sorted(rows.items()) if owner == execution_id}

    def forget_viewers(self, slug: str, panes: list[str]) -> None:
        if panes:
            self.redis.hdel(self.store.key(slug, VIEWERS), *panes)
