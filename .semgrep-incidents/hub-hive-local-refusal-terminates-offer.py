from typing import Any


class Admission:
    def admit(self, slug: str, request: dict[str, Any]) -> dict[str, Any] | None:
        # ruleid: hub-hive-local-refusal-terminates-offer
        if older := self._outdated(request["hive"]):
            self.refused(slug, request, older)
            return None
        # ruleid: hub-hive-local-refusal-terminates-offer
        if unplaceable := self._unplaceable(request["task"], request["hive"]):
            self.refused(slug, request, unplaceable)
            return None
        return self._claim(slug, request)


class Held:
    class Admission:
        def admit(self, slug: str, request: dict[str, Any]) -> dict[str, Any] | None:
            # ok: hub-hive-local-refusal-terminates-offer
            if older := self._outdated(request["hive"]):
                return {"held": older}
            # ok: hub-hive-local-refusal-terminates-offer
            if unplaceable := self._unplaceable(request["task"], request["hive"]):
                return {"held": unplaceable}
            if self._withdrawn(request["task"]):
                return None
            return self._claim(slug, request)
