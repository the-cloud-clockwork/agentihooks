import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

CONTEXT = "context.sha"


def source_path(filename: str, work: Path, root: Path, tracked: set[str] | None = None) -> str | None:
    path = Path(filename).resolve()
    for prefix in (work.resolve(), root.resolve()):
        if path.is_relative_to(prefix):
            relative = path.relative_to(prefix).as_posix()
            if (tracked is None or relative in tracked) and (root / relative).is_file():
                return relative
    return None


def measured_dependencies(
    coverage_file: Path, work: Path, root: Path, nodeids: list[str], tracked: set[str] | None = None
) -> dict[str, list[str]]:
    from coverage import CoverageData

    data = CoverageData(basename=str(coverage_file))
    data.read()
    shared, found = set(), {}
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if isinstance(filename, str) and (path := source_path(filename, work, root, tracked)):
            shared.add(path)
    for filename in sorted(data.measured_files()):
        if not (path := source_path(filename, work, root, tracked)):
            continue
        contexts = {context for values in data.contexts_by_lineno(filename).values() for context in values}
        if "" in contexts:
            shared.add(path)
        for context in filter(None, contexts):
            found.setdefault(context.rpartition("|")[0], set()).add(path)
    if not all(node in found for node in nodeids):
        return {}
    return {node: sorted(found[node] | shared) for node in nodeids}


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@dataclass
class StoredStats:
    root: Path
    folder: Path
    context: dict
    changes: dict
    misses: Counter = field(default_factory=Counter)
    changed: Counter = field(default_factory=Counter)

    def pointer(self, path: str) -> Path:
        context = {name: value for name, value in self.context.items() if name != "inventory"}
        return self.folder / f"{digest({'context': context, 'path': path})}.index"

    def context_digest(self) -> str:
        return digest({name: value for name, value in self.context.items() if name != "inventory"})

    def scope(self, inputs: dict) -> dict:
        return {source: lines for source, lines in self.changes.items() if source in inputs}

    def identity(self, path: str, inputs: dict, scope: dict, measured: bool, discovery: str = "") -> str:
        context = {name: value for name, value in self.context.items() if name != "inventory"}
        return digest(
            {
                "context": context,
                "path": path,
                "inputs": inputs,
                "scope": scope,
                "measured": measured,
                "discovery": discovery,
            }
        )

    @staticmethod
    def read(record_file: Path) -> dict | None:
        try:
            return json.loads(record_file.read_text())
        except ValueError:
            record_file.unlink()
            return None

    def prune(self) -> None:
        for record_file in self.folder.glob("*.json"):
            self.read(record_file)

    def load(self, path: str, check: Callable[[dict, bool, str], bool] | None = None) -> dict | None:
        pointer = self.pointer(path)
        if not pointer.is_file():
            self.misses["no pointer"] += 1
            return None
        key = pointer.read_text()
        if not re.fullmatch(r"[0-9a-f]{64}", key) or not (record_file := self.folder / f"{key}.json").is_file():
            self.misses["no record"] += 1
            return None
        if (record := self.read(record_file)) is None:
            self.misses["no record"] += 1
            return None
        inputs, scope, measured = record["inputs"], record["scope"], record["measured"]
        discovery = record.get("discovery", "")
        if (
            record["key"] != key
            or record["status"] != 0
            or key != self.identity(path, inputs, scope, measured, discovery)
        ):
            self.misses["identity"] += 1
            return None
        if not inputs.keys() <= set(self.context["inventory"]) or scope != self.scope(inputs):
            self.misses["scope"] += 1
            return None
        changed = [
            name
            for name, fingerprint in inputs.items()
            if not (self.root / name).is_file()
            or hashlib.sha256((self.root / name).read_bytes()).hexdigest() != fingerprint
        ]
        if changed:
            self.misses["inputs"] += 1
            self.changed.update(changed)
            return None
        if check is not None and not check(inputs, measured, discovery):
            self.misses["check"] += 1
            return None
        result = {
            name: value
            for name, value in record.items()
            if name not in {"key", "inputs", "scope", "measured", "discovery", "context", "observed_at"}
        }
        return {**result, "reused": True} if measured else result

    def save(self, path: str, result: dict, inputs: dict, measured: bool, discovery: str = "") -> None:
        from time import time_ns

        scope = self.scope(inputs)
        key = self.identity(path, inputs, scope, measured, discovery)
        record = {
            **result,
            "key": key,
            "inputs": inputs,
            "scope": scope,
            "measured": measured,
            "discovery": discovery,
            "context": self.context_digest(),
            "observed_at": time_ns(),
        }
        (self.folder / f"{key}.json").write_text(json.dumps(record))
        self.pointer(path).write_text(key)
        (self.folder / CONTEXT).write_text(record["context"])
