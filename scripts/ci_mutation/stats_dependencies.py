import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from coverage import CoverageData


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

    def pointer(self, path: str) -> Path:
        return self.folder / f"{digest({'context': self.context, 'path': path})}.index"

    def scope(self, inputs: dict) -> dict:
        return {source: lines for source, lines in self.changes.items() if source in inputs}

    def identity(self, path: str, inputs: dict, scope: dict, measured: bool) -> str:
        return digest({"context": self.context, "path": path, "inputs": inputs, "scope": scope, "measured": measured})

    def load(self, path: str) -> dict | None:
        pointer = self.pointer(path)
        if not pointer.is_file():
            return None
        key = pointer.read_text()
        if not re.fullmatch(r"[0-9a-f]{64}", key) or not (record_file := self.folder / f"{key}.json").is_file():
            return None
        record = json.loads(record_file.read_text())
        inputs, scope, measured = record["inputs"], record["scope"], record["measured"]
        if record["key"] != key or record["status"] != 0 or key != self.identity(path, inputs, scope, measured):
            return None
        if not inputs.keys() <= set(self.context["inventory"]) or scope != self.scope(inputs):
            return None
        if any(
            not (self.root / name).is_file()
            or hashlib.sha256((self.root / name).read_bytes()).hexdigest() != fingerprint
            for name, fingerprint in inputs.items()
        ):
            return None
        result = {name: value for name, value in record.items() if name not in {"key", "inputs", "scope", "measured"}}
        return {**result, "reused": True} if measured else result

    def save(self, path: str, result: dict, inputs: dict, measured: bool) -> None:
        scope = self.scope(inputs)
        key = self.identity(path, inputs, scope, measured)
        record = {**result, "key": key, "inputs": inputs, "scope": scope, "measured": measured}
        (self.folder / f"{key}.json").write_text(json.dumps(record))
        self.pointer(path).write_text(key)
