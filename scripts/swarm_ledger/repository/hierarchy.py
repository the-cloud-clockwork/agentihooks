SCHEMA = """
CREATE TABLE IF NOT EXISTS work_nodes (ledger_slug TEXT NOT NULL REFERENCES ledgers(slug) ON DELETE CASCADE, node_id TEXT NOT NULL, kind TEXT NOT NULL, parent_id TEXT, position INTEGER NOT NULL, PRIMARY KEY(ledger_slug,node_id));
CREATE INDEX IF NOT EXISTS work_nodes_parent ON work_nodes(ledger_slug,parent_id,position);
CREATE TABLE IF NOT EXISTS work_dependencies (ledger_slug TEXT NOT NULL, node_id TEXT NOT NULL, requires_id TEXT NOT NULL, PRIMARY KEY(ledger_slug,node_id,requires_id), FOREIGN KEY(ledger_slug,node_id) REFERENCES work_nodes(ledger_slug,node_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS work_dependencies_required ON work_dependencies(ledger_slug,requires_id);
"""
KINDS = {"plans": "plan", "phases": "phase", "slices": "slice", "tasks": "task"}
LINKS = {"phases": "plan", "slices": "phase", "tasks": "slice"}
NODES = "SELECT node_id, kind, parent_id, position FROM work_nodes WHERE ledger_slug=?"
DEPENDENCIES = "SELECT node_id, requires_id FROM work_dependencies WHERE ledger_slug=?"
DELETE_NODE = "DELETE FROM work_nodes WHERE ledger_slug=? AND node_id=?"
UPSERT_NODE = (
    "INSERT INTO work_nodes VALUES (?, ?, ?, ?, ?) ON CONFLICT(ledger_slug,node_id) DO UPDATE SET "
    "kind=excluded.kind, parent_id=excluded.parent_id, position=excluded.position"
)
DELETE_DEPENDENCY = "DELETE FROM work_dependencies WHERE ledger_slug=? AND node_id=? AND requires_id=?"
INSERT_DEPENDENCY = "INSERT OR IGNORE INTO work_dependencies VALUES (?, ?, ?)"


def text(value) -> bool:
    return isinstance(value, str) and bool(value)


def parent(collection: str, item: dict) -> str | None:
    found = item.get(LINKS.get(collection))
    if collection == "tasks" and not text(found) and text(item.get("phase")):
        found = f"phases/{item['phase']}"
    return found if text(found) else None


def project(state: dict) -> tuple[dict, set]:
    nodes, dependencies = {}, set()
    for collection, kind in KINDS.items():
        for position, item in enumerate(state.get(collection) or []):
            if not isinstance(item, dict) or not text(item.get("id")):
                continue
            node = f"{collection}/{item['id']}"
            nodes[node] = (kind, parent(collection, item), position)
            required = item.get("depends_on")
            dependencies.update(
                (node, f"{collection}/{other}")
                for other in (required if isinstance(required, list) else [])
                if text(other)
            )
    return nodes, dependencies


def stored(connection, slug: str) -> tuple[dict, set]:
    nodes = {
        node: (kind, parent_id, position) for node, kind, parent_id, position in connection.execute(NODES, (slug,))
    }
    return nodes, set(connection.execute(DEPENDENCIES, (slug,)))


def apply(connection, slug: str, old: tuple, new: tuple) -> None:
    (old_nodes, old_dependencies), (new_nodes, new_dependencies) = old, new
    connection.executemany(DELETE_NODE, [(slug, node) for node in old_nodes if node not in new_nodes])
    connection.executemany(
        UPSERT_NODE, [(slug, node, *row) for node, row in new_nodes.items() if old_nodes.get(node) != row]
    )
    connection.executemany(DELETE_DEPENDENCY, [(slug, *edge) for edge in old_dependencies - new_dependencies])
    connection.executemany(INSERT_DEPENDENCY, [(slug, *edge) for edge in new_dependencies - old_dependencies])


def sync(connection, slug: str, state: dict) -> None:
    apply(connection, slug, stored(connection, slug), project(state))


def drift(have: tuple, want: tuple) -> dict:
    (have_nodes, have_dependencies), (want_nodes, want_dependencies) = have, want
    report = {
        "missing_nodes": sorted(node for node in want_nodes if node not in have_nodes),
        "extra_nodes": sorted(node for node in have_nodes if node not in want_nodes),
        "changed_nodes": sorted(
            node for node in want_nodes if node in have_nodes and have_nodes[node] != want_nodes[node]
        ),
        "missing_dependencies": sorted(list(edge) for edge in want_dependencies - have_dependencies),
        "extra_dependencies": sorted(list(edge) for edge in have_dependencies - want_dependencies),
    }
    return {**report, "drift": sum(len(found) for found in report.values())}


def rebuild(connection, slug: str, state: dict) -> dict:
    have, want = stored(connection, slug), project(state)
    apply(connection, slug, have, want)
    return drift(have, want)


def ordering(alias: str) -> str:
    kind = f"CASE {alias}.kind WHEN 'plan' THEN 0 WHEN 'phase' THEN 1 WHEN 'slice' THEN 2 ELSE 3 END"
    return f"printf('%d.%09d', {kind}, {alias}.position)"


ROOT = (
    "(n.parent_id IS :node OR (:node IS NULL AND n.parent_id NOT IN "
    "(SELECT node_id FROM work_nodes WHERE ledger_slug=:slug)))"
)
# The bounds end the walks on a malformed parent or dependency loop; a plan sits at most three levels above a task.
LEVELS = 3
CHAIN = 64
CHILDREN = (
    f"SELECT n.node_id, n.kind, n.parent_id, 1 FROM work_nodes n WHERE n.ledger_slug=:slug AND {ROOT} "
    f"ORDER BY {ordering('n')}"
)
SUBTREE = f"""
WITH RECURSIVE tree(node_id, kind, parent_id, depth, sort) AS (
  SELECT n.node_id, n.kind, n.parent_id, 0, {ordering("n")} FROM work_nodes n
  WHERE n.ledger_slug=:slug AND (n.node_id=:node OR (:node IS NULL AND {ROOT}))
  UNION ALL
  SELECT n.node_id, n.kind, n.parent_id, tree.depth + 1, tree.sort || '/' || {ordering("n")}
  FROM work_nodes n JOIN tree ON n.ledger_slug=:slug AND n.parent_id=tree.node_id WHERE tree.depth < {LEVELS}
)
SELECT node_id, kind, parent_id, depth FROM tree ORDER BY sort
"""
ANCESTORS = f"""
WITH RECURSIVE up(node_id, kind, parent_id, depth) AS (
  SELECT node_id, kind, parent_id, 0 FROM work_nodes WHERE ledger_slug=:slug AND node_id=:node
  UNION ALL
  SELECT n.node_id, n.kind, n.parent_id, up.depth + 1
  FROM work_nodes n JOIN up ON n.ledger_slug=:slug AND n.node_id=up.parent_id WHERE up.depth < {LEVELS}
)
SELECT node_id, kind, parent_id, depth FROM up WHERE depth > 0 ORDER BY depth DESC
"""
DEPENDENTS = f"""
WITH RECURSIVE down(node_id, depth) AS (
  SELECT node_id, 1 FROM work_dependencies WHERE ledger_slug=:slug AND requires_id=:node
  UNION
  SELECT d.node_id, down.depth + 1
  FROM work_dependencies d JOIN down ON d.ledger_slug=:slug AND d.requires_id=down.node_id WHERE down.depth < {CHAIN}
)
SELECT n.node_id, n.kind, n.parent_id, MIN(down.depth) AS nearest
FROM down JOIN work_nodes n ON n.ledger_slug=:slug AND n.node_id=down.node_id
GROUP BY n.node_id ORDER BY nearest, {ordering("n")}
"""
READS = {"children": CHILDREN, "subtree": SUBTREE, "ancestors": ANCESTORS, "dependents": DEPENDENTS}
NODE = "SELECT 1 FROM work_nodes WHERE ledger_slug=? AND node_id=?"


def read(connection, slug: str, name: str, node: str | None = None) -> list:
    if node is not None and connection.execute(NODE, (slug, node)).fetchone() is None:
        raise KeyError(node)
    rows = connection.execute(READS[name], {"slug": slug, "node": node})
    return [
        {"node": found, "kind": kind, "parent": parent_id, "depth": depth} for found, kind, parent_id, depth in rows
    ]
