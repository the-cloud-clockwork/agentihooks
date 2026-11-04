"""Ordering only dependencies: the tick records the dependency audit each pass, and status lists it with one finding each."""

import json
from typing import Any

from scripts.swarm import timing
from scripts.swarm.health.findings import Finding
from scripts.swarm.store import RedisStore
from scripts.swarm_ledger import dependency_audit

KEY = "dependency-audit"
EVIDENCE = (
    "their territories share no code file",
    "the dependent plan lines name no code of the task it waits on",
)


def observe(store: RedisStore, slug: str, doc: dict[str, Any]) -> list[dict[str, str]]:
    """Audit the ledger's open dependencies and save the ordering only ones for status."""
    found = dependency_audit.audit(doc.get("tasks", []), dependency_audit.plan_text(doc))
    timing.keep()
    store.redis.set(store.key(slug, KEY), json.dumps(found))  # pyright: ignore[reportUnknownMemberType]
    return found


def read(store: RedisStore, slug: str) -> list[dict[str, str]]:
    """The ordering only dependencies the last tick pass saved."""
    raw = store.redis.get(store.key(slug, KEY))  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return json.loads(raw) if isinstance(raw, str) else []


def findings(store: RedisStore, slug: str) -> list[Finding]:
    """One finding per saved ordering only dependency, so the master drops it."""
    return [
        Finding(
            "ordering only dependency",
            f"{row['task']}-after-{row['depends_on']}",
            f"task {row['title']} depends on {row['dependency_title']}, "
            "but the two share no code and its plan names none of that code",
            EVIDENCE,
            "a dependency with no shared or named code",
            1,
            evidence_identity=(row["task"], row["depends_on"]),
        )
        for row in read(store, slug)
    ]
