"""The warm ledger writer answers every refusal and leaves every audit effect over its socket as the in process call
does, on the SQLite test store and on the shared Postgres database the Ledger writer lock workflow provides."""

import io
import json
import os
import threading
import time
import urllib.error
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from typing import Any

import psycopg2
import pytest

from hooks import _redis
from scripts.gates import progress, talk
from scripts.swarm_ledger import (
    api_service,
    ledger_server,
    ledger_task_duplicates,
    request_metrics,
    storage,
    warm_writer,
)
from scripts.swarm_ledger.api import resources, schemas
from scripts.swarm_ledger.api.client import ResourceClient
from scripts.swarm_ledger.repository import postgres
from scripts.tick.adapters import database_ledger

URL = os.environ.get("LEDGER_TEST_POSTGRES_URL", "")
STORES = ["sqlite", "postgres"] if URL else ["sqlite"]
SHARED = ["postgres"] if URL else []
CONTENT = {"title": "Parity", "overview": "o", "sources": [], "phases": [{"title": "One", "description": "d"}]}
THREAD = "phases/p1/comments"
STALE = "0" * 64
WORKER = "engineer@323133-0440"
CLOCKS = frozenset({"at", "time"})
TASK = {
    "op": "task_add",
    "id": "parity-task",
    "by": "swarm",
    "task": "t1",
    "title": "Parity task",
    "lane": "eng",
    "phase": "p1",
}
REPEAT = {**TASK, "id": "parity-repeat", "task": "t2"}
COMMENT = {"op": "add", "id": "parity-comment", "by": "swarm", "thread": THREAD, "text": "Landed"}
TALK = {"op": "add", "id": "parity-talk", "by": WORKER, "thread": THREAD, "text": "Beyond the budget"}
SLIPPED = {"op": "add", "id": "parity-slipped", "by": "swarm", "thread": THREAD, "text": "Slipped in"}
ACK = {"op": "events_ack", "id": "parity-ack", "by": "swarm", "rev": 1}
MATCH = {"kind": "task", "id": "t1", "title": "Parity task", "state": "open", "rank": None, "phase_title": "One"}
GENERATIONS = "SELECT slug, generation FROM ledgers WHERE slug IN (%s, %s) ORDER BY slug"


@dataclass
class Twins:
    """Two ledgers made from the same content in one store, the write hooks each fired with the state it received,
    and the reply and request metric samples each request left."""

    store: Any
    here: str
    there: str
    fired: list[tuple[str, str, Any]] = field(default_factory=list)
    samples: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    replies: dict[str, tuple[Any, ...]] = field(default_factory=dict)

    def hooks(self, slug: str) -> list[tuple[str, Any]]:
        """The write hooks fired for ``slug`` with the state each received, in order."""
        return [(hook, alike(state, slug)) for hook, fired, state in self.fired if fired == slug]


@dataclass(frozen=True)
class Refusal:
    """One refused write: the caller sending it, its operations, the status it answers, the words only its own refusal
    leaves in the reply or the ledger, whether it names an operation id, whether its guards are stale and what arms
    the refusal."""

    caller: str
    ops: tuple[dict[str, Any], ...]
    status: int
    reason: str
    identified: bool = True
    stale: bool = False
    arm: Callable[[pytest.MonkeyPatch], None] | None = None


def clocked(key: str) -> bool:
    """Whether ``key`` names a clock reading: a time, an at or any moment ending in _at."""
    return key in CLOCKS or key.endswith("_at")


def unclocked(value: Any) -> Any:
    """``value`` with every clock reading blanked, since twin ledgers are written moments apart."""
    if isinstance(value, dict):
        return {key: None if clocked(key) else unclocked(item) for key, item in value.items()}
    if isinstance(value, list):
        return [unclocked(item) for item in value]
    return value


def alike(value: Any, slug: str) -> Any:
    """``value`` with its clock readings blanked and its own ledger's slug named alike."""
    return unclocked(json.loads(json.dumps(value).replace(slug, "<slug>")))


def bound(name: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The repository both transports write: the shared SQLite test store, or the shared Postgres database bound into
    the ledger server the route code reads its repository from."""
    if name == "sqlite":
        return storage.default()
    shared = postgres.PostgresLedgerRepository(URL)
    shared.migrate()
    monkeypatch.setattr(ledger_server, "repository", ledger_server.publishing(shared, ledger_server.publish_snapshot))
    return shared


@pytest.fixture(params=STORES)
def store(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Each repository the parity holds on; Postgres only where its test address is set."""
    return bound(request.param, monkeypatch)


@pytest.fixture
def twins(store: Any, monkeypatch: pytest.MonkeyPatch) -> Twins:
    """Twin ledgers in ``store``, their inbox relay, doctor phrase and alert hooks recorded with the state each got."""
    stem = f"parity-{uuid.uuid4().hex[:8]}"
    found = Twins(store, f"{stem}-here", f"{stem}-there")
    for slug in (found.here, found.there):
        store.create(slug, CONTENT, size="swarm")
    for hook in ("relay_to_inbox", "doctor_phrase", "deliver_alerts"):
        monkeypatch.setattr(api_service, hook, lambda slug, state, hook=hook: found.fired.append((hook, slug, state)))
    monkeypatch.setattr(ledger_server.authority, "fence", lambda slug, epoch: None)
    monkeypatch.setattr(_redis, "get_redis", lambda *args, **kwargs: None)
    monkeypatch.setattr(talk.Budget, "_marks", lambda self: None)
    monkeypatch.setattr(ledger_task_duplicates, "find", lambda doc, adds, kind="task": [None] * len(adds))
    return found


def same_revision(root: Path | str) -> str:
    """The revision of a checkout that never moves."""
    return "same"


def paced(interval: float, timeout: float | None = None) -> None:
    """A scheduler wait the writer paces its checkout checks on, slept for its interval."""
    time.sleep(interval)


def answering(where: Path) -> bool:
    """Whether a writer accepts connections on ``where``."""
    try:
        warm_writer.connected(1.0, where).close()
    except OSError:
        return False
    return True


@pytest.fixture
def writer(store: Any, memory_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A warm writer over ``store`` answering on a socket in a short tmpfs folder."""
    monkeypatch.setattr(warm_writer, "revision", same_revision)
    monkeypatch.setattr(ledger_server, "scheduled_wait", paced)
    where = memory_path / "p.sock"
    stopped = threading.Event()
    served = threading.Thread(target=warm_writer.serve, args=(where, stopped, 0.05), daemon=True)
    served.start()
    deadline = time.monotonic() + 10.0
    while not answering(where) and time.monotonic() < deadline:
        time.sleep(0.01)
    yield where
    stopped.set()
    served.join(10.0)


def over_socket(where: Path, slug: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, bytes]:
    """The status and body the writer on ``where`` answers one request with."""
    return warm_writer.ask(warm_writer.connected(10.0, where), slug, path, payload)


def in_process(slug: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, bytes]:
    """The status and body the in process call answers the same request with."""
    _, status, body = database_ledger.replied(slug, path, payload, database_ledger.written(path))
    return status, body


def sent(twins: Twins, slug: str, send: Callable[[], tuple[int, bytes]]) -> tuple[Any, ...]:
    """What one request gives its caller, its status and body or the error it raised, keeping the request metric
    samples it left for ``slug``."""
    before = len(request_metrics.SAMPLES)
    try:
        status, body = send()
    except Exception as exc:
        found: tuple[Any, ...] = ("raised", type(exc).__name__, str(exc).replace(slug, "<slug>"))
    else:
        found = ("answered", status, alike(json.loads(body), slug))
    twins.samples[slug] = [(row[1], row[2]) for row in list(request_metrics.SAMPLES)[before:]]
    twins.replies[slug] = found
    return found


def both(twins: Twins, writer: Path, payload: Callable[[str], dict[str, Any]]) -> tuple[Any, Any]:
    """What one write gives its caller in process on the first twin and over the socket on the second."""
    expected = sent(twins, twins.here, lambda: in_process(twins.here, "operations", payload(twins.here)))
    answered = sent(twins, twins.there, lambda: over_socket(writer, twins.there, "operations", payload(twins.there)))
    return expected, answered


def guarded(store: Any, slug: str, ops: tuple[dict[str, Any], ...]) -> dict[str, str]:
    """The write guards naming the revision each path ``ops`` write holds in ``slug``."""
    held, _ = store.apply_ops(slug)
    targets = {schemas.target(op) for op in ops if op["op"] != "task_add"}
    return {path: resources.resource_revision(held, path) for path in sorted(targets)}


def state(store: Any, slug: str) -> Any:
    """The ledger state ``store`` holds for ``slug``, named alike."""
    held, _ = store.apply_ops(slug)
    return alike(held, slug)


def rows(store: Any, slug: str, sql: str) -> list[Any]:
    """Every row ``sql`` selects for ``slug`` from ``store``, its JSON values decoded and named alike."""
    with store.connect() as connection:
        found = connection.execute(sql, (slug,)).fetchall()
    return [
        alike(
            [json.loads(item) if isinstance(item, str) and item.startswith(("[", "{")) else item for item in row], slug
        )
        for row in found
    ]


def events(store: Any, slug: str) -> list[Any]:
    """The event rows ``store`` holds for ``slug``."""
    return rows(store, slug, "SELECT key, revision, position, value FROM events WHERE slug=? ORDER BY position")


def outbox(store: Any, slug: str) -> list[Any]:
    """The outbox relay rows ``store`` holds for ``slug``, each envelope's own id blanked."""
    found = rows(store, slug, "SELECT revision, subject, value FROM outbox WHERE slug=? ORDER BY revision, subject")
    return [[revision, subject, {**value, "id": None}] for revision, subject, value in found]


def duplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the duplicate finder match every add to the ledger's first task."""
    monkeypatch.setattr(ledger_task_duplicates, "find", lambda doc, adds, kind="task": [MATCH] * len(adds))


class Spent:
    """Progress marks of a worker that spent its talk budget after its last outcome."""

    def read(self, agent: str) -> progress.Mark:
        """The worker's marks: an outcome, then the whole budget talked."""
        return progress.Mark(1, "pushed", talk.BUDGET)


def over_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the write gate's talk budget enforce on a worker that has spent it."""
    monkeypatch.setattr(talk.Budget, "_marks", lambda self: ("enforce", Spent()))
    monkeypatch.setattr(talk.Budget, "_exempt", lambda self, doc, by, ctx: False)


REFUSALS = {
    "schema": Refusal("", (COMMENT,), 400, '"code": "schema_invalid"', identified=False),
    "authority": Refusal("eng@ab-1", (COMMENT,), 403, '"code": "forbidden"'),
    "duplicate": Refusal("", (REPEAT,), 200, 'task t2 repeats task t1 \\"Parity task\\"', arm=duplicated),
    "write-gate": Refusal(
        "", (TALK,), 200, f"{talk.REFUSED}: {WORKER} made {talk.BUDGET} talk writes", arm=over_budget
    ),
    "revision": Refusal("", (COMMENT,), 409, '"code": "revision_conflict"', stale=True),
}


@pytest.mark.feature("ledger-warm-writer")
@pytest.mark.parametrize("case", list(REFUSALS.values()), ids=list(REFUSALS))
def test_each_refusal_answers_alike_over_the_socket_and_in_process(
    twins: Twins, writer: Path, monkeypatch: pytest.MonkeyPatch, case: Refusal
) -> None:
    if case.arm is not None:
        case.arm(monkeypatch)

    def payload(slug: str) -> dict[str, Any]:
        guards = {THREAD: STALE} if case.stale else guarded(twins.store, slug, case.ops)
        named = {"operation_id": "parity-op"} if case.identified else {}
        return {**named, "ops": [dict(op) for op in case.ops], "guards": guards}

    with database_ledger.acting(case.caller):
        expected, answered = both(twins, writer, payload)
    assert expected[:2] == ("answered", case.status)
    assert answered == expected
    assert case.reason in json.dumps([expected, state(twins.store, twins.here)])
    if case.status == 200:
        assert expected[2]["applied"] == []
        assert expected[2]["rejected"] == [case.ops[0]["id"]]
    assert state(twins.store, twins.there) == state(twins.store, twins.here)
    assert events(twins.store, twins.there) == events(twins.store, twins.here)
    assert outbox(twins.store, twins.there) == outbox(twins.store, twins.here)
    assert twins.hooks(twins.there) == twins.hooks(twins.here)


def run(sql: str, parameters: tuple[Any, ...] = ()) -> list[tuple[Any, ...]] | None:
    """The rows one statement returns on the shared Postgres database, committed."""
    with closing(psycopg2.connect(URL)) as raw, raw, raw.cursor() as cursor:
        cursor.execute(sql, parameters)
        return cursor.fetchall() if cursor.description else None


@pytest.fixture
def raised_floor() -> Iterator[int]:
    """The shared database's writer floor raised one version above this writer's."""
    run("UPDATE writer_floor SET version = %s WHERE name = 'ledger'", (postgres.VERSION + 1,))
    yield postgres.VERSION + 1
    run("UPDATE writer_floor SET version = %s WHERE name = 'ledger'", (postgres.REQUIRED,))


@pytest.mark.parametrize("store", SHARED, indirect=True)
def test_a_writer_below_the_floor_is_refused_alike_over_the_socket_and_in_process(
    twins: Twins, writer: Path, raised_floor: int
) -> None:
    generations = run(GENERATIONS, (twins.here, twins.there))

    def payload(slug: str) -> dict[str, Any]:
        return {"operation_id": "parity-op", "ops": [dict(TASK)], "guards": {}}

    with database_ledger.acting(""):
        expected, answered = both(twins, writer, payload)
    refusal = postgres.refusal(postgres.VERSION, raised_floor)
    assert expected == ("raised", "Refused", refusal)
    assert answered == expected
    assert run(GENERATIONS, (twins.here, twins.there)) == generations
    assert run("SELECT version FROM writer_floor WHERE name = 'ledger'") == [(raised_floor,)]


def answered_over(where: Path) -> Callable[[str, str, dict[str, Any] | None], dict[str, Any]]:
    """A ledger client transport that sends each request to the writer on ``where``, a refusal raised as the in
    process transport raises it."""

    def send(slug: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        status, body = over_socket(where, slug, path, payload)
        if status >= 400:
            url = f"/api/v1/ledgers/{slug}/{path}"
            raise urllib.error.HTTPError(url, status, "ledger refused", Message(), io.BytesIO(body))
        reply: dict[str, Any] = json.loads(body)
        return reply

    return send


def slipping(
    store: Any, send: Callable[[str, str, dict[str, Any] | None], dict[str, Any]], calls: list[tuple[str, int]]
) -> Callable[[str, str, dict[str, Any] | None], dict[str, Any]]:
    """A transport over ``send`` that slips one comment into the thread in process before the first write lands,
    keeping the path and status of every request it sends."""

    def request(slug: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if path == "operations" and ("operations", 409) not in calls:
            slip = {"operation_id": "parity-slip", "ops": [dict(SLIPPED)], "guards": guarded(store, slug, (SLIPPED,))}
            database_ledger.answered(slug, "operations", slip)
        try:
            reply = send(slug, path, payload)
        except urllib.error.HTTPError as exc:
            calls.append((path, exc.code))
            raise
        calls.append((path, 200))
        return reply

    return request


@pytest.mark.feature("ledger-warm-writer")
def test_a_conflicting_write_retries_once_and_lands_alike_over_the_socket_and_in_process(
    twins: Twins, writer: Path
) -> None:
    here_calls: list[tuple[str, int]] = []
    there_calls: list[tuple[str, int]] = []
    in_process_client = ResourceClient("", {}, 10, transport=slipping(twins.store, database_ledger.request, here_calls))
    socket_client = ResourceClient("", {}, 10, transport=slipping(twins.store, answered_over(writer), there_calls))
    with database_ledger.acting(""):
        expected = in_process_client.mutate(twins.here, [{**COMMENT, "operation_id": "parity-retry"}])
        answered = socket_client.mutate(twins.there, [{**COMMENT, "operation_id": "parity-retry"}])
    assert here_calls == [(THREAD, 200), ("operations", 409), (THREAD, 200), ("operations", 200)]
    assert there_calls == here_calls
    assert alike(answered, twins.there) == alike(expected, twins.here)
    assert expected["applied"] == ["parity-comment"]
    assert state(twins.store, twins.there) == state(twins.store, twins.here)
    assert events(twins.store, twins.there) == events(twins.store, twins.here)
    assert outbox(twins.store, twins.there) == outbox(twins.store, twins.here)


def acked(twins: Twins, slug: str) -> Any:
    """The events ack the ledger keeps for ``slug``."""
    held, _ = twins.store.apply_ops(slug)
    return held["_meta"].get("events_ack")


EFFECTS: dict[str, Callable[[Twins, str], Any]] = {
    "events": lambda twins, slug: events(twins.store, slug),
    "outbox": lambda twins, slug: outbox(twins.store, slug),
    "request-metrics": lambda twins, slug: twins.samples[slug],
    "inbox-relay": lambda twins, slug: twins.hooks(slug),
    "ack": acked,
    "write-ack": lambda twins, slug: twins.replies[slug],
}
FILLED = {"events", "outbox", "inbox-relay", "ack", "write-ack"}


@pytest.mark.feature("ledger-warm-writer")
@pytest.mark.parametrize("effect", list(EFFECTS))
def test_each_audit_effect_is_left_alike_over_the_socket_and_in_process(
    twins: Twins, writer: Path, effect: str
) -> None:
    ops = (TASK, COMMENT, ACK)

    def payload(slug: str) -> dict[str, Any]:
        return {"operation_id": "parity-op", "ops": [dict(op) for op in ops], "guards": guarded(twins.store, slug, ops)}

    with database_ledger.acting(""):
        expected, answered = both(twins, writer, payload)
    assert expected[:2] == ("answered", 200)
    assert expected[2]["applied"] == [op["id"] for op in ops]
    assert answered == expected
    read = EFFECTS[effect]
    assert read(twins, twins.there) == read(twins, twins.here)
    if effect in FILLED:
        assert read(twins, twins.here) not in (None, [])
    else:
        assert read(twins, twins.here) == []
    assert state(twins.store, twins.there) == state(twins.store, twins.here)
