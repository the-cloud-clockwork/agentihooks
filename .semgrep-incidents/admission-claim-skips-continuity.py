from scripts.handoff import transfers
from scripts.swarm import tick


class NoContinuity:
    def _claim(self, slug: str, seat: str, row: dict) -> dict:
        # ruleid: admission-claim-skips-continuity
        task = tick.primed(self.store, slug, seat, row)
        return task


class HandoffOnly:
    def _claim(self, slug: str, seat: str, row: dict) -> dict:
        handoff = self.store.handoff(slug, row["id"])
        # ruleid: admission-claim-skips-continuity
        task = tick.primed(self.store, slug, seat, {**row, "handoff": handoff})
        return task


class TransferOnly:
    def _claim(self, slug: str, seat: str, row: dict, record: object) -> dict:
        # ruleid: admission-claim-skips-continuity
        task = tick.primed(self.store, slug, seat, row)
        task["transfer"] = transfers.attach(self.store, slug, record)
        return task


class Continuity:
    def _claim(self, slug: str, seat: str, row: dict, record: object) -> dict:
        handoff = self.store.handoff(slug, row["id"])
        # ok: admission-claim-skips-continuity
        task = tick.primed(self.store, slug, seat, {**row, "handoff": handoff})
        task["transfer"] = transfers.attach(self.store, slug, record)
        return task
