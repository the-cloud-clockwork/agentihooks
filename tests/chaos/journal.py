import json
import time
from pathlib import Path


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class Journal:
    def __init__(self, path: Path, proc: str):
        self.proc = proc
        self._file = open(path, "a", buffering=1)

    def write(self, kind: str, **fields) -> None:
        self._file.write(json.dumps({"t": now_ms(), "proc": self.proc, "kind": kind, **fields}) + "\n")
        self._file.flush()


def read(folder: Path) -> list[dict]:
    records = []
    for path in sorted(folder.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return sorted(records, key=lambda record: record["t"])
