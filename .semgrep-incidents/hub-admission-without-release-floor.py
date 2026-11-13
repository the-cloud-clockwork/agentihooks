from typing import Any, Protocol


class Admitter(Protocol):
    def admit(self, slug: str, request: dict[str, Any]) -> dict[str, Any] | None: ...


class Admission(Admitter):
    # ruleid: hub-admission-without-release-floor
    def admit(self, slug: str, request: dict[str, Any]) -> dict[str, Any] | None:
        if not self._placeable(request["hive"]):
            return None
        return self._claim(slug, request)


class Floored:
    class Admission(Admitter):
        # ok: hub-admission-without-release-floor
        def admit(self, slug: str, request: dict[str, Any]) -> dict[str, Any] | None:
            if older := self._outdated(request["hive"]):
                print(older)
                return None
            return self._claim(slug, request)
