from typing import Any


class SnapshotRead:
    source: Any

    def admit(self, slug: str) -> object:
        # ruleid: hub-admission-reads-full-export
        return self.source.snapshot(slug)


class ExportRead:
    ledger: Any

    def claim(self, slug: str) -> object:
        # ruleid: hub-admission-reads-full-export
        return self.ledger.state(slug)


class NarrowRead:
    view: Any

    def admit(self, slug: str, task_id: str) -> object:
        # ok: hub-admission-reads-full-export
        return self.view.task(slug, task_id, print)


class PlanDocExport:
    _full: Any

    def _doc(self, slug: str, row: dict[str, Any]) -> object:
        if row.get("kind") == "plan":
            # ruleid: hub-admission-reads-full-export
            return self._full.state(slug)
        return {}


class PlanDocTasksCollection:
    _ledger: Any

    def _steering(self, slug: str, phase_id: str) -> object:
        # ruleid: hub-admission-reads-full-export
        rows = self._ledger.tasks(slug)
        # ruleid: hub-admission-reads-full-export
        document = self._ledger.export(slug)
        return [row for row in rows if row.get("phase") == phase_id], document


class PlanDocNarrow:
    _ledger: Any

    def _steering(self, slug: str, phase_id: str) -> object:
        # ok: hub-admission-reads-full-export
        phase = self._ledger.item(slug, f"phases/{phase_id}")
        # ok: hub-admission-reads-full-export
        comments = self._ledger.thread(slug, f"phases/{phase_id}", "comments")
        # ok: hub-admission-reads-full-export
        tasks = self._ledger.tasks_in_phases(slug, [phase_id], ("title", "state"))
        # ok: hub-admission-reads-full-export
        return phase, comments, tasks, self._ledger.artifacts(slug)
