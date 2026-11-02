import hashlib
import json
from pathlib import Path
from typing import Any


def digest(document: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()


def _write(path: Path | str, record: dict[str, Any]) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(record, indent=2) + "\n")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _replayed(record: dict[str, Any], operation: str, sha256: str, error: type[ValueError]) -> dict[str, Any] | None:
    done: dict[str, Any] | None = next((o for o in record["operations"] if o["id"] == operation), None)
    if done and done["sha256"] != sha256:
        raise error(f"operation {operation} was already recorded with different content")
    return done
