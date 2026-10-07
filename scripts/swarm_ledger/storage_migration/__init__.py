import json
from pathlib import Path

from scripts.swarm_ledger.repository.file import core, load_state
from scripts.swarm_ledger.repository.shadow import storage_lock
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository


def import_directory(directory: Path, database: Path | None = None) -> list:
    directory = Path(directory)
    repository = SQLiteLedgerRepository(database or directory / "ledger-shadow.sqlite3")
    registries = {}
    imported = []
    with storage_lock(directory):
        for name, filename in (("bin", ".bin.json"), ("restored", ".bin-restored.json")):
            path = directory / filename
            registries[name] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            repository.import_registry(name, registries[name])
        for page in sorted(directory.glob("*.html")):
            path = page.with_suffix(".json")
            seed = None if path.exists() else core.parse_seed(page.read_text(encoding="utf-8"))
            document, meta, _ = load_state(path, seed, core)
            repository.import_document(
                path.stem,
                {**document, "_meta": meta},
                registries["bin"].get(path.stem),
                registries["restored"].get(path.stem),
            )
            imported.append(path.stem)
    return imported
