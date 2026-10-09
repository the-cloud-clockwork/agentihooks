import hashlib
import json

from scripts.swarm import metrics_outbox
from scripts.swarm.ledger_client import LedgerClient

PATH_KEYS = ("plan", "phase", "slice", "task")
EMPTY_PATH = dict.fromkeys(PATH_KEYS, "")
STATES = ("open", "claimed", "blocked", "pr", "done", "out_of_scope")
EVENTS = metrics_outbox.Table(
    "ledger_events",
    (
        ("revision", "Int64"),
        ("kind", "String"),
        ("by", "String"),
        ("target", "String"),
        ("lane", "String"),
        ("state", "String"),
        ("catch_up", "Int64"),
        ("first_missed", "Int64"),
        ("last_missed", "Int64"),
        ("payload", "String"),
    ),
)
CHECKPOINT = "CREATE TABLE IF NOT EXISTS ledger_metrics (slug TEXT PRIMARY KEY, revision INTEGER, snapshot_ms INTEGER)"
READ_CHECKPOINT = "SELECT revision, snapshot_ms FROM ledger_metrics WHERE slug=?"
SAVE_CHECKPOINT = "INSERT OR REPLACE INTO ledger_metrics VALUES (?, ?, ?)"
PATHS = "CREATE TABLE IF NOT EXISTS ledger_metric_paths (slug TEXT, node TEXT, value TEXT, PRIMARY KEY (slug, node))"
READ_PATHS = "SELECT node, value FROM ledger_metric_paths WHERE slug=?"
SAVE_PATHS = "INSERT OR REPLACE INTO ledger_metric_paths VALUES (?, ?, ?)"


def paths(nodes: list, tasks: list) -> dict:
    parents = {row["node"]: row for row in nodes}
    lanes = {f"tasks/{task['id']}": task.get("lane", "eng") for task in tasks}
    result = {}
    for row in nodes:
        path, node = EMPTY_PATH.copy(), row["node"]
        for _ in PATH_KEYS:
            ancestor = parents.get(node)
            if ancestor is None:
                break
            path[ancestor["kind"]] = node
            node = ancestor["parent"]
        result[row["node"]] = {**path, "lane": lanes.get(row["node"], "")}
    return result


def base(slug: str, ts_ms: int, identity: str, path: dict) -> dict:
    return {"event_id": identity, "ledger": slug, "ts_ms": ts_ms, **EMPTY_PATH, **path}


def event_row(slug: str, event: dict, path: dict, catch_up: bool) -> dict:
    payload = json.dumps(event, sort_keys=True)
    identity = hashlib.sha256(payload.encode()).hexdigest()
    state = event["kind"].removeprefix("task ")
    return {
        **base(slug, event["at"], f"ledger:{slug}:{identity}", path),
        "revision": event["rev"],
        "kind": event["kind"],
        "by": event["by"],
        "target": event["target"],
        "lane": path.get("lane", ""),
        "state": state if state in STATES else "",
        "catch_up": int(catch_up),
        "first_missed": 0,
        "last_missed": 0,
        "payload": payload,
    }


def event_rows(slug: str, events: list, known: dict, cursor: int | None, now_ms: int) -> list:
    rows = []
    if events and cursor is not None and events[0]["rev"] > cursor + 1:
        first, last = cursor + 1, events[0]["rev"] - 1
        gap = {"rev": last, "at": now_ms, "by": "metrics", "kind": "history gap", "target": ""}
        row = event_row(slug, gap, {}, False)
        rows.append({**row, "first_missed": first, "last_missed": last})
    for event in events:
        if cursor is None or event["rev"] > cursor:
            rows.append(event_row(slug, event, known.get(event["target"], {}), cursor is None))
    return rows


def record(box: metrics_outbox.Outbox, slug: str, now_ms: int, ledger: LedgerClient) -> None:
    doc, nodes = ledger.state(slug), ledger.hierarchy(slug)
    box.db.execute(CHECKPOINT)
    box.db.execute(PATHS)
    checkpoint = box.db.execute(READ_CHECKPOINT, (slug,)).fetchone()
    cursor = None if checkpoint is None else checkpoint[0]
    known = {node: json.loads(value) for node, value in box.db.execute(READ_PATHS, (slug,))}
    current = paths(nodes, doc.get("tasks", []))
    known.update(current)
    rows = event_rows(slug, doc["_meta"]["events"], known, cursor, now_ms)
    box.append(EVENTS, rows)
    with box.db:
        box.db.executemany(SAVE_PATHS, [(slug, node, json.dumps(path)) for node, path in current.items()])
        box.db.execute(SAVE_CHECKPOINT, (slug, doc["_meta"]["rev"], 0))
