import hashlib
import json
from pathlib import Path


def clearance_path(root: Path, key: str) -> Path:
    return root / "mutation-clearances" / f"{hashlib.sha256(key.encode()).hexdigest()}.json"


def load_clearances(root: Path) -> dict:
    legacy = root / "mutation-cleared.txt"
    cleared = json.loads(legacy.read_text()) if legacy.exists() else {}
    for path in sorted((root / "mutation-clearances").glob("*.json")):
        record = json.loads(path.read_text())
        if len(record) != 1:
            raise ValueError("Mutation clearance file requires one mutant identity")
        key, ruling = next(iter(record.items()))
        if path != clearance_path(root, key):
            raise ValueError("Mutation clearance filename does not match its identity")
        if key in cleared and cleared[key] != ruling:
            raise ValueError(f"Conflicting mutation clearance: {key}")
        cleared[key] = ruling
    return cleared


def write_clearance(root: Path, key: str, ruling: dict) -> None:
    target = clearance_path(root, key)
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps({key: ruling}, indent=2) + "\n")


def migrate(root: Path) -> int:
    load_clearances(root)
    legacy = root / "mutation-cleared.txt"
    records = json.loads(legacy.read_text())
    for key, ruling in records.items():
        write_clearance(root, key, ruling)
    legacy.write_text("{}\n")
    return len(records)


if __name__ == "__main__":
    print(f"Migrated {migrate(Path.cwd())} mutation clearances")
