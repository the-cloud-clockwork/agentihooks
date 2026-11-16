"""A ledger of task rows for review state that applies a task update only while its if_match fields still hold."""

import copy
from typing import Any

GUARD_DEFAULTS: dict[str, Any] = {"review_readers": [], "review_rounds": {}}


class ReviewLedger:
    def __init__(self, rows: dict[str, dict[str, Any]] | None = None, slug: str = "sw") -> None:
        self.slug = slug
        self.rows: dict[str, dict[str, Any]] = rows if rows is not None else {"t1": {"id": "t1"}}
        self.writes: list[dict[str, Any]] = []

    def task(self, slug: str, task_id: str) -> dict[str, Any] | None:
        assert slug == self.slug
        return copy.deepcopy(self.rows.get(task_id))

    def update_task(
        self,
        slug: str,
        task_id: str,
        fields: dict[str, Any],
        by: str = "swarm",
        if_state: tuple[str, ...] = (),
        if_match: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert slug == self.slug and not if_state
        row = self.rows[task_id]
        self.writes.append(copy.deepcopy({"task": task_id, "fields": fields, "by": by, "if_match": if_match}))
        if all(row.get(key, GUARD_DEFAULTS.get(key, "")) == value for key, value in (if_match or {}).items()):
            row.update(copy.deepcopy(fields))
        return copy.deepcopy(row)
