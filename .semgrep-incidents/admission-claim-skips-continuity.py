from scripts.handoff import transfers
from scripts.swarm import tick


class NoTransfer:
    def _claim(self, slug: str, seat: str, row: dict) -> dict:
        handoff = self.store.handoff(slug, row["id"])
        # ruleid: admission-claim-skips-continuity
        task = tick.primed(self.store, slug, seat, {**row, "handoff": handoff})
        return task


class TransferElsewhere:
    def _claim(self, slug: str, seat: str, row: dict) -> dict:
        # ruleid: admission-claim-skips-continuity
        return tick.primed(self.store, slug, seat, row)

    def reopen(self, slug: str, record: object) -> object:
        return transfers.attach(self.store, slug, record)


class Attached:
    def _claim(self, slug: str, seat: str, row: dict, record: object) -> dict:
        # ok: admission-claim-skips-continuity
        task = tick.primed(self.store, slug, seat, row)
        task["transfer"] = transfers.attach(self.store, slug, record)
        return task
