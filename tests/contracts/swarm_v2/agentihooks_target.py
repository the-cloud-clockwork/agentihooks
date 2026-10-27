import json
from pathlib import Path

import scripts.swarm_v2.contracts as contracts

STATUS = {"invalid_request": 400, "forbidden_scope": 403, "stale_generation": 409, "revision_conflict": 409}


class Target:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.contracts = contracts.load()

    def submit(self, contract: str, doc: dict, grant: dict) -> tuple[int, dict]:
        body = contracts.admit(self.contracts, self.root, contract, doc, grant=grant)
        return STATUS.get(body.get("error"), 200), body

    def snapshot(self) -> dict[str, str]:
        return {path.relative_to(self.root).as_posix(): path.read_text() for path in sorted(self.root.rglob("*.json"))}

    def stored(self, contract: str, operation_id: str) -> dict | None:
        path = self.root / contract / f"{operation_id}.json"
        return json.loads(path.read_text()) if path.exists() else None
