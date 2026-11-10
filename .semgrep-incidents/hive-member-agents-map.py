from typing import Any


def legacy_fixture(name: str, digest: str) -> dict[str, Any]:
    def mint() -> dict[str, Any]:
        # ruleid: hive-member-agents-map
        return {"name": name, "ledger": digest, "agents": {}}

    return mint()


def compact_fixture(name: str, digest: str) -> dict[str, Any]:
    def mint() -> dict[str, Any]:
        # ok: hive-member-agents-map
        return {"name": name, "ledger": digest}

    return mint()
