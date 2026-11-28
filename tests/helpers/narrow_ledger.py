"""Test ledgers answering the narrow reads a hub admission makes from one ledger document, through the ledger API's
own task selection and focus verdict code, so a fake never judges a gate its own way."""

import time
from collections.abc import Callable, Iterable
from typing import Any, cast

from scripts.swarm import admission_view
from scripts.swarm.health import focus_check
from scripts.swarm_ledger.api import resources


class NarrowReads:
    """The task, phase, metadata, selection, thread, artifact and focus reads of the document ``document(slug)``
    returns."""

    def __init__(self, document: Callable[[str], dict[str, Any]], fix: str = "") -> None:
        self.document, self.fix = document, fix

    def _row(self, slug: str, collection: str, item_id: str) -> dict[str, Any] | None:
        rows: list[dict[str, Any]] = self.document(slug).get(collection) or []
        found = next((row for row in rows if row.get("id") == item_id), None)
        return None if found is None else {**cast(dict[str, Any], resources.project(found))}

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

    def tasks_in_phases(self, slug: str, phases: Iterable[str], fields: Iterable[str] = ()) -> list[dict[str, Any]]:
        from scripts.swarm.ledger_client import where_query
        from scripts.swarm_ledger.api import task_list

        reply = task_list.selecting(self.document(slug)["tasks"], where_query(phases=phases, fields=fields))
        found: list[dict[str, Any]] = reply["data"]
        return found

    def thread(self, slug: str, record: str, name: str) -> list[dict[str, Any]]:
        collection, _, item_id = record.partition("/")
        rows: list[dict[str, Any]] = self.document(slug).get(collection) or []
        found = next((row for row in rows if row.get("id") == item_id), {})
        return [dict(entry) for entry in found.get(name, [])]

    def artifacts(self, slug: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.document(slug).get("artifacts") or []]

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


class Viewed(NarrowReads):
    """The narrow reads of the document in the snapshot an admission's test source hands out for each swarm; every
    other call reaches ``inner``."""

    def __init__(self, admitter: Any, inner: Any) -> None:
        super().__init__(lambda slug: admitter.source.snapshot(slug).doc)
        self.inner = inner

    def state(self, slug: str) -> dict[str, Any]:
        return self.document(slug)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def viewed(admitter: Any) -> Any:
    """Points ``admitter``'s narrow view at the snapshots its test source hands out, read when the view judges: the
    reads answer from each snapshot's document and the claimed and admits checks are the snapshot's own, so a rig that
    swaps its source keeps steering the admission."""
    reads = Viewed(admitter, admitter.ledger)
    admitter.view = admission_view.Narrow(
        lambda: admitter.store,
        reads,
        cast(Any, reads),
        claims=lambda _store, slug: admitter.source.snapshot(slug).claimed,
        admitting=lambda _store, slug, _config, _rows: admitter.source.snapshot(slug).admits,
    )
    return admitter


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
