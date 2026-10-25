"""Stand down one managed Codex conversation: fence its execution, then interrupt its active turn, with no signal."""

import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING

from scripts.codex_session import protocol
from scripts.codex_session.binding import (
    EXITED,
    FAILED,
    PUBLISHING,
    RECORDS,
    STOOD_DOWN,
    BindingStore,
    TransportRecord,
    decode,
)
from scripts.codex_session.supervisor import UNIX

if TYPE_CHECKING:
    from scripts.terminate_agent import Session

POLL_S = 0.2
WAIT_S = 10.0
CONNECT_S = 5.0
IN_PROGRESS = "inProgress"
LISTING = "agentihooks terminate-agent --list --type codex shows the live conversations"
RETRY = "run the same stand down again to interrupt its turn"
NOT_FENCED = "is not fenced; run the same stand down again"


class Refused(ValueError):
    def __init__(self, reason: str, alternative: str) -> None:
        super().__init__(reason, alternative)
        self.reason, self.alternative = self.args


class NotFenced(RuntimeError):
    pass


@dataclass(frozen=True)
class Plan:
    slug: str
    record: TransportRecord
    stops: tuple[str, ...]
    keeps: tuple[str, ...]


def _conversation(record: TransportRecord) -> str:
    return f"conversation {record.thread_id} of {record.agent_name}"


def _ended(record: TransportRecord) -> Refused:
    return Refused(f"{_conversation(record)} already ended ({record.state})", "nothing needs standing down")


def _unmanaged(slug: str, selector: str, sessions: "list[Session]") -> Refused:
    reason = (
        f"{selector} has no managed Codex conversation in swarm {slug}, "
        "so its conversations cannot be stopped one at a time"
    )
    pids = {s.process.pid for s in sessions if selector in {s.name, s.session_id, str(s.process.pid)}}
    if len(pids) != 1:
        return Refused(reason, LISTING)
    [pid] = pids
    ids = ", ".join(sorted(s.session_id for s in sessions if s.process.pid == pid and s.session_id))
    hosted = ids or "that agentihooks has not registered, so which ones is unknown"
    return Refused(
        reason,
        f"process {pid} hosts conversations {hosted}; "
        f"agentihooks terminate-agent {pid} --force-shared --dry-run names its process group, "
        "and the same command without --dry-run ends every conversation in that process, "
        "so run it only when all of them belong to this agent",
    )


def plan(bindings: BindingStore, slug: str, selector: str, sessions: "list[Session]") -> Plan:
    rows = bindings.redis.hgetall(bindings.store.key(slug, RECORDS)).values()
    matches = [
        record
        for record in map(decode, rows)
        if selector in {record.agent_name, record.thread_id, record.session_id, record.execution_id}
    ]
    if not matches:
        raise _unmanaged(slug, selector, sessions)
    live = [record for record in matches if record.state not in {EXITED, FAILED}]
    if not live:
        raise _ended(matches[0])
    if len(live) > 1:
        threads = ", ".join(sorted(record.thread_id for record in live))
        raise Refused(f"{selector} names {len(live)} live conversations", f"name one thread: {threads}")
    [record] = live
    own = {record.thread_id, record.session_id}
    keeps = (f"app-server pid {record.server_pid}: not signalled",) + tuple(
        f"conversation {s.session_id} of {s.name}: not interrupted"
        for s in sessions
        if s.process.pid == record.server_pid and s.session_id and s.session_id not in own
    )
    stops = (
        f"{_conversation(record)} (execution {record.execution_id} generation {record.execution_generation}): "
        f"its active turn (last recorded: {record.active_turn_id or 'none'}) is interrupted "
        "and it takes no new turns",
    )
    return Plan(slug, record, stops, keeps)


def _active(connection: protocol.Connection, thread_id: str) -> str:
    last = (protocol.read_thread(connection, thread_id).get("turns") or [{}])[-1]
    return last["id"] if last.get("status") == IN_PROGRESS else ""


def _settle(
    until: Callable[[], bool], clock: Callable[[], float], sleep: Callable[[float], None], wait_s: float
) -> bool:
    deadline = clock() + wait_s
    while not until():
        if clock() >= deadline:
            return False
        sleep(POLL_S)
    return True


def _fence(bindings: BindingStore, slug: str, record: TransportRecord) -> None:
    from scripts.inbox.receipts import transact

    key = bindings.store.key(slug, RECORDS)

    def fence(pipe):
        pipe.watch(key)
        current = bindings.get(slug, record.execution_id)
        if current.state in {EXITED, FAILED}:
            raise _ended(current)
        pipe.multi()
        pipe.hset(key, record.execution_id, json.dumps(asdict(replace(current, state=STOOD_DOWN))))

    transact(bindings.redis, fence)


def execute(
    bindings: BindingStore,
    chosen: Plan,
    connect: Callable = protocol.connect,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wait_s: float = WAIT_S,
) -> dict:
    from redis.exceptions import RedisError

    record = chosen.record
    try:
        _fence(bindings, chosen.slug, record)
    except RedisError as exc:
        raise NotFenced(str(exc)) from exc
    publishing = bindings.store.key(chosen.slug, PUBLISHING)
    if not _settle(lambda: bindings.redis.hget(publishing, record.seat) != record.execution_id, clock, sleep, wait_s):
        raise RuntimeError(f"an inbox publication for {record.agent_name} is still in flight")
    connection, _ = connect(record.endpoint_ref.removeprefix(UNIX), CONNECT_S)
    try:
        turn = _active(connection, record.thread_id)
        if turn:
            protocol.interrupt_turn(connection, record.thread_id, turn)
            if not _settle(lambda: _active(connection, record.thread_id) != turn, clock, sleep, wait_s):
                raise RuntimeError(f"turn {turn} still runs after its interrupt")
    finally:
        connection.close()
    return {"stood_down": record.agent_name, "thread": record.thread_id, "interrupted": turn}


def _choose(selector: str, swarm: str | None, sessions: "list[Session]") -> tuple[BindingStore, Plan]:
    from scripts.swarm import store as swarm_store

    store = swarm_store.connect()
    slug = swarm or store.names.slug_of(selector)
    if not slug:
        raise Refused(f"no swarm given and {selector} names no swarm agent", "pass --swarm <slug>")
    bindings = BindingStore(store)
    return bindings, plan(bindings, slug, selector, sessions)


def _refuse(refused: Refused) -> int:
    print(f"terminate-agent: refused: {refused.reason}", file=sys.stderr)
    print(f"terminate-agent: safe alternative: {refused.alternative}", file=sys.stderr)
    return 2


def _failed(exc: Exception, status: str) -> int:
    print(f"terminate-agent: {exc}", file=sys.stderr)
    print(f"terminate-agent: {status}", file=sys.stderr)
    return 1


def _finish(report: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(report))
        return 0
    rest = {key: value for key, value in report.items() if key not in {"action", "stops", "keeps", "result"}}
    print(" ".join([f"result={report['result']}", *(f"{key}={value}" for key, value in rest.items())]))
    return 0


def run(selector: str, swarm: str | None, dry_run: bool, sessions: "list[Session]", as_json: bool) -> int:
    from redis.exceptions import RedisError

    try:
        bindings, chosen = _choose(selector, swarm, sessions)
    except Refused as refused:
        return _refuse(refused)
    except RedisError as exc:
        return _failed(exc, f"{selector} {NOT_FENCED}")
    report = {
        "action": f"stand-down{'-dry-run' if dry_run else ''}",
        "stops": chosen.stops,
        "keeps": chosen.keeps,
    }
    if not as_json:
        print(f"action={report['action']}")
        for line in chosen.stops:
            print(f"stops: {line}")
        for line in chosen.keeps:
            print(f"keeps: {line}")
    if dry_run:
        return _finish({**report, "result": "validated", "signal": "none"}, as_json)
    name = chosen.record.agent_name
    try:
        result = execute(bindings, chosen)
    except Refused as refused:
        return _refuse(refused)
    except NotFenced as exc:
        return _failed(exc, f"{name} {NOT_FENCED}")
    except (OSError, TimeoutError, RuntimeError, RedisError, protocol.ProtocolError) as exc:
        return _failed(exc, f"{name} stays fenced from new inbox turns; {RETRY}")
    return _finish({**report, "result": "stood-down", **result}, as_json)
