from typing import Any


class Legacy:
    def mint(self, name: str, digest: str) -> dict[str, Any]:
        # ruleid: hive-member-agents-map
        return {"name": name, "ledger": digest, "agents": {}}


class Compact:
    def mint(self, name: str, digest: str) -> dict[str, Any]:
        # ok: hive-member-agents-map
        return {"name": name, "ledger": digest}
