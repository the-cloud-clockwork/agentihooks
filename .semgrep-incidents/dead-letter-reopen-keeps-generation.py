from typing import Any, Protocol


class DeadLetter(Protocol):
    def _finish_dead(self, event: Any) -> None: ...


class ReopensPlainly(DeadLetter):
    def _finish_dead(self, event: Any) -> None:
        self.bus.dead.publish("dead.rig", event)
        # ruleid: dead-letter-reopen-keeps-generation
        self.tasks.reopen(event)


class AsksDeadLettered(DeadLetter):
    def _finish_dead(self, event: Any) -> None:
        self.bus.dead.publish("dead.rig", event)
        # ok: dead-letter-reopen-keeps-generation
        self.tasks.dead_lettered(event)


class Reopen(Protocol):
    def reopen(self, slug: str, request: dict) -> bool: ...


class ForgetsUnheld(Reopen):
    def reopen(self, slug: str, request: dict) -> bool:
        held = self.store.redis.get(slug)
        # ruleid: dead-letter-reopen-keeps-generation
        if not held or held["hive"] != request["hive"]:
            return False
        return True


class AdvancesUnheld(Reopen):
    def reopen(self, slug: str, request: dict) -> bool:
        held = self.store.redis.get(slug)
        # ok: dead-letter-reopen-keeps-generation
        if not held:
            if request.get("dead"):
                self._advance(slug, request)
            return False
        if held["hive"] != request["hive"]:
            return False
        return True
