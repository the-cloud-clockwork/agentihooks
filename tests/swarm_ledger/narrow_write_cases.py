import json
import uuid
from collections.abc import Callable
from typing import Any

FEATURE = "ledger-task-update-narrow-write"
CLAIMANT = "engineer@abc123-0001"
WORKSPACE = "~/.agentihooks/swarm/narrow/tasks/t1"
TABLES = ("fields", "resources", "threads")
SETTLE = {"op": "add", "id": "settle", "thread": "chat", "text": "settle"}
HOUR_MS = 3600000
CONTENT = {
    "title": "Narrow",
    "phases": [{"title": "Proof", "description": "d"}],
    "tasks": [{"title": f"task {index}", "phase": "p1", "lane": "eng"} for index in range(4)],
}


def claim_op(task: str = "t1", **extra: Any) -> dict:
    op = {
        "op": "task_update",
        "id": "claim",
        "by": "swarm",
        "item": f"tasks/{task}",
        "fields": {"state": "claimed", "claimed_by": CLAIMANT, "workspace": WORKSPACE},
        "if_state": ["open"],
        "if_match": {"claimed_by": "", "claim_generation": 0},
    }
    return {**op, **extra}


def rows_of(repository: Any, slug: str) -> dict:
    found: dict = {}
    with repository.connect() as connection:
        for table in TABLES:
            found[table] = sorted(
                connection.execute(
                    f"SELECT path, parent, key, position, kind, value FROM {table} WHERE slug=?", (slug,)
                ).fetchall()
            )
        found["events"] = sorted(
            connection.execute("SELECT key, revision, position, value FROM events WHERE slug=?", (slug,)).fetchall()
        )
        found["outbox"] = sorted(
            (subject, revision, json.loads(value)["type"], json.dumps(json.loads(value)["data"], sort_keys=True))
            for subject, revision, value in connection.execute(
                "SELECT subject, revision, value FROM outbox WHERE slug=?", (slug,)
            ).fetchall()
        )
        found["row_changes"] = sorted(
            paths for (paths,) in connection.execute("SELECT paths FROM row_changes WHERE slug=?", (slug,)).fetchall()
        )
        found["ledgers"] = [
            (revision, generation, summary)
            for revision, generation, summary in connection.execute(
                "SELECT revision, generation, summary FROM ledgers WHERE slug=?", (slug,)
            ).fetchall()
        ]
    return json.loads(
        json.dumps({key: [list(row) for row in rows] for key, rows in found.items()}).replace(slug, "slug")
    )


def outcomes(document: dict) -> None:
    document["_meta"]["outcomes"] = {"t1": {"state": "done"}}


def sync_alert(item: str, at: int) -> dict:
    return {
        "id": "al-0-0",
        "text": "an earlier refusal",
        "source": "sync",
        "target": "swarm",
        "state": "open",
        "at": at,
        "rev": 1,
        "writer": "swarm",
        "item": item,
        "last_refused_at": at,
    }


def recovered(document: dict) -> None:
    document["alerts"].append(sync_alert("tasks/t1", document["_meta"]["updated_at"]))


def expired(document: dict) -> None:
    document["alerts"].append(sync_alert("tasks/t3", document["_meta"]["updated_at"] + 1000 - HOUR_MS))


def dependency(document: dict) -> None:
    document["tasks"][1]["depends_on"] = ["t1"]


def protected(document: dict) -> None:
    document["tasks"][0].update(branch="main", repository_branches={"agentihooks": "main", "other": "x"})


def small(document: dict) -> None:
    document["size"] = "small"


def pull(document: dict) -> None:
    document["tasks"][0]["pr_url"] = "https://github.com/example/repo/pull/1"


def stamped(document: dict) -> None:
    at = document["_meta"]["updated_at"]
    document["_meta"]["stamps"]["tasks/t1/state"] = {"at": at, "rev": 1, "by": "boss"}


def blocked(document: dict) -> None:
    document["tasks"][0]["state"] = "blocked"


def finished(document: dict) -> None:
    document["tasks"][0].update(state="done", done=True)


def undismissed(document: dict) -> None:
    del document["_meta"]["priorities_dismissed"]


FREED = {
    "op": "task_update",
    "id": "free",
    "by": "swarm",
    "item": "tasks/t1",
    "fields": {"state": "open", "claimed_by": ""},
}
SCENARIOS: dict[str, tuple[dict, Callable[[dict], None] | None, list]] = {
    "first claim": (claim_op(), None, []),
    "state guard miss": (claim_op(), None, [[claim_op(id="first")]]),
    "owner guard miss": (claim_op(if_match={"claimed_by": "engineer@abc123-0009", "claim_generation": 0}), None, []),
    "committed outcome refused": (claim_op(), outcomes, []),
    "recovered sync alert": (claim_op(), recovered, []),
    "expired sync alert": (claim_op(), expired, []),
    "open dependency held": (claim_op("t2"), dependency, []),
    "protected branch cleared": (
        {**claim_op(), "fields": {"state": "claimed", "claimed_by": CLAIMANT, "branch": ""}},
        protected,
        [],
    ),
    "small ledger": (claim_op(), small, []),
    "pull request claim": ({**claim_op(), "fields": {"state": "pr", "claimed_by": CLAIMANT}}, pull, []),
    "stamped before": (claim_op(), stamped, []),
    "claim after release": (
        claim_op(if_match={"claimed_by": "", "claim_generation": 2}),
        None,
        [[claim_op(id="first")], [FREED]],
    ),
}
FALLBACKS = (blocked, finished, undismissed)


class Clock:
    def __init__(self) -> None:
        self.at = 1795400000000

    def __call__(self) -> int:
        return self.at

    def tick(self, by: int = 1000) -> None:
        self.at += by


def created(repository: Any, slug: str, prepare: Callable[[dict], None] | None) -> None:
    assert repository.create(slug, CONTENT, size="swarm")
    if prepare is not None:
        document = repository.export_document(slug)
        prepare(document)
        repository.import_document(slug, document, replace=True)


def settled(pairs: list, clock: Clock) -> None:
    clock.tick(500)
    for repository, slug in pairs:
        repository.apply_ops(slug, ops=[SETTLE])


def twins(make: Callable[[str], Any], prepare: Callable[[dict], None] | None, monkeypatch: Any) -> tuple:
    whole, narrow = make("whole"), make("narrow")
    clock = Clock()
    monkeypatch.setattr(whole.domain, "now_ms", clock)
    left, right = f"whole-{uuid.uuid4().hex[:8]}", f"narrow-{uuid.uuid4().hex[:8]}"
    created(whole, left, prepare)
    created(narrow, right, prepare)
    settled([(whole, left), (narrow, right)], clock)
    return whole, narrow, left, right, clock


def assert_same_write(make: Callable[[str], Any], scenario: str, monkeypatch: Any) -> None:
    assert_same(make, SCENARIOS[scenario], monkeypatch)


def assert_same(make: Callable[[str], Any], case: tuple, monkeypatch: Any) -> None:
    op, prepare, before = case
    whole, narrow, left, right, clock = twins(make, prepare, monkeypatch)
    for ops in before:
        whole.apply_ops(left, ops=ops)
        narrow.apply_ops(right, ops=ops)
    clock.tick()
    _, rejected = whole.apply_ops(left, ops=[op])
    written = narrow.apply_task_update(right, op)
    assert written is not None
    assert written.rejected == rejected
    assert rows_of(narrow, right) == rows_of(whole, left)
    assert narrow.get_document(right) == make("narrow").get_document(right) == whole.get_document(left)


def assert_falls_back(
    make: Callable[[str], Any], prepare: Callable[[dict], None], monkeypatch: Any, op: dict | None = None
) -> None:
    repository = make("fallback")
    clock = Clock()
    monkeypatch.setattr(repository.domain, "now_ms", clock)
    slug = f"fallback-{uuid.uuid4().hex[:8]}"
    created(repository, slug, None)
    settled([(repository, slug)], clock)
    document = repository.export_document(slug)
    prepare(document)
    repository.import_document(slug, document, replace=True)
    before = rows_of(repository, slug)
    assert repository.apply_task_update(slug, op or claim_op()) is None
    assert rows_of(repository, slug) == before
