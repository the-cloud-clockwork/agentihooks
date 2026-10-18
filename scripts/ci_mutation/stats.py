import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class SharedStats:
    folder: Path
    head: str
    part: tuple[int, int] | None = None

    def group(self, index: int) -> "SharedStats":
        return replace(self, folder=self.folder / f"group-{index}")


def stats_key(head: str, selected: dict[str, tuple[set[int], list[str]]]) -> str:
    files = {path: [sorted(lines), sorted(tests)] for path, (lines, tests) in selected.items()}
    return hashlib.sha256(json.dumps({"head": head, "files": files}, sort_keys=True).encode()).hexdigest()


def write_part(path: Path, key: str, part: tuple[int, int], results: list[dict]) -> None:
    index, total = part
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"key": key, "part": index, "parts": total, "results": results}))


def load_parts(folder: Path, key: str) -> tuple[list[dict], str]:
    files = sorted(folder.glob("part-*.json")) if folder.is_dir() else []
    if not files:
        return [], f"mutation stats missing: no stats part in {folder}"
    parts = [json.loads(file.read_text()) for file in files]
    if stale := sorted(part["part"] for part in parts if part["key"] != key):
        return [], f"mutation stats stale: parts {stale} were collected for another commit or selection"
    totals = {part["parts"] for part in parts}
    found = sorted(part["part"] for part in parts)
    if len(totals) != 1 or found != list(range(max(totals))):
        return [], f"mutation stats missing: found parts {found} of {sorted(totals)}"
    return [result for part in parts for result in part["results"]], ""
