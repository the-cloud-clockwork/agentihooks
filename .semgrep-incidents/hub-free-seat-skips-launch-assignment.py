from scripts.swarm import tick


class HandoffOnly:
    def _claim(self, slug: str, lane: str, task_id: str, taken: set[str]) -> str:
        # ruleid: hub-free-seat-skips-launch-assignment
        return tick._free_seat(slug, lane, taken, self.store.handoff_seat(slug, task_id))


class AssignmentElsewhere:
    def _claim(self, slug: str, lane: str, taken: set[str]) -> str:
        # ruleid: hub-free-seat-skips-launch-assignment
        return tick._free_seat(slug, lane, taken, "")

    def launched(self, slug: str, task_id: str) -> None:
        self.store.redis.hdel(self.store.key(slug, "launch-assignments"), task_id)


class SavedSeatFirst:
    def _claim(self, slug: str, lane: str, task_id: str, taken: set[str]) -> str:
        saved = self.store.redis.hget(self.store.key(slug, "launch-assignments"), task_id)
        preferred = saved or self.store.handoff_seat(slug, task_id)
        # ok: hub-free-seat-skips-launch-assignment
        return tick._free_seat(slug, lane, taken, preferred)
