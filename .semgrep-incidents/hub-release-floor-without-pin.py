from typing import Any


class Admission:
    # ruleid: hub-release-floor-without-pin
    def _outdated(self, hive: str) -> str:
        record: dict[str, Any] = self._record(hive)
        return "older" if record.get("version") else ""

    def _record(self, hive: str) -> dict[str, Any]:
        return {"hive": hive}


class Pinned:
    class Admission:
        # ok: hub-release-floor-without-pin
        def _outdated(self, hive: str) -> str:
            record: dict[str, Any] = self._record(hive)
            if record.get("pinned") == "yes":
                return ""
            return "older" if record.get("version") else ""

        def _record(self, hive: str) -> dict[str, Any]:
            return {"hive": hive}
