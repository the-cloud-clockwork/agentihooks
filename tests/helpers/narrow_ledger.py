"""Test ledgers answering the narrow reads a hub admission makes from one ledger document, through the ledger API's
own task selection and focus verdict code, so a fake never judges a gate its own way."""

import time
from collections.abc import Callable
from typing import Any

from scripts.swarm.health import focus_check
from scripts.swarm_ledger.api import resources


class NarrowReads:
    """The task, phase, metadata, selection and focus reads of the document ``document(slug)`` returns."""

    def __init__(self, document: Callable[[str], dict[str, Any]], fix: str = "") -> None:
        self.document, self.fix = document, fix

    def _row(self, slug: str, collection: str, item_id: str) -> dict[str, Any] | None:
        rows: list[dict[str, Any]] = self.document(slug).get(collection) or []
        found = next((row for row in rows if row.get("id") == item_id), None)
        return None if found is None else dict(resources.project(found))  # pyright: ignore[reportArgumentType]

    def task(self, slug: str, task_id: str) -> dict[str, Any] | None:
        return self._row(slug, "tasks", task_id)

    def item(self, slug: str, path: str) -> dict[str, Any] | None:
        collection, _, item_id = path.partition("/")
        return self._row(slug, collection, item_id)

    def metadata(self, slug: str) -> dict[str, Any]:
        document = self.document(slug)
        return {"overview": document.get("overview", ""), "_meta": dict(document.get("_meta") or {"rev": 1})}

    def tasks_where(self, slug: str, **filters: Any) -> list[dict[str, Any]]:
        from scripts.swarm.ledger_client import where_query
        from scripts.swarm_ledger.api import task_list

        reply = task_list.selecting(self.document(slug)["tasks"], where_query(**filters))
        found: list[dict[str, Any]] = reply["data"]
        return found

    def focus_verdicts(self, slug: str, target: str) -> dict[str, Any] | None:
        found = focus_check.read(self.document(slug), f"focus/{target}", self.fix, int(time.time() * 1000))
        return found if isinstance(found, dict) else None


class Reads(NarrowReads):
    """The narrow reads of a fake ledger answered from its own export; every other call reaches the fake."""

    def __init__(self, inner: Any, fix: str = "") -> None:
        super().__init__(inner.state, fix)
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class DocLedger(NarrowReads):
    """One in memory ledger document: narrow reads answer from it, the whole export is counted and can move a fake
    clock, the metadata revision can move on every read, and task updates hold their state and match guards."""

    def __init__(
        self,
        document: dict[str, Any],
        moving: bool = False,
        export: Callable[[], None] = lambda: None,
    ) -> None:
        super().__init__(lambda _slug: document)
        self.doc, self.moving, self.export = document, moving, export
        self.rev, self.exports = 1, 0
        self.writes: list[tuple[str, str, dict[str, Any], tuple[str, ...], dict[str, Any] | None]] = []

    def metadata(self, slug: str) -> dict[str, Any]:
        self.rev += 1 if self.moving else 0
        return {"overview": self.doc.get("overview", ""), "_meta": {"rev": self.rev}}

    def state(self, slug: str) -> dict[str, Any]:
        self.exports += 1
        self.export()
        return self.doc

    def update_task(
        self,
        slug: str,
        task_id: str,
        fields: dict[str, Any],
        if_state: tuple[str, ...] = (),
        if_match: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.writes.append((slug, task_id, fields, tuple(if_state), if_match))
        row = next(row for row in self.doc["tasks"] if row["id"] == task_id)
        matched = all(
            row.get(key, "" if key == "claimed_by" else 0) == value for key, value in (if_match or {}).items()
        )
        if row["state"] in if_state and matched:
            if fields.get("claimed_by", row.get("claimed_by", "")) != row.get("claimed_by", ""):
                row["claim_generation"] = row.get("claim_generation", 0) + 1
            row.update(fields)
        return dict(row)
