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
