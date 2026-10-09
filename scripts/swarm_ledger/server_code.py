"""The ledger server's code folders and the code stamp it loaded."""

import json
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
ROOT = CODE_DIR.parents[1]
CODE_DIRS = (
    CODE_DIR,
    *(ROOT / "scripts" / name for name in ("inbox", "swarm", "handoff", "doctor", "gates", "hive")),
    ROOT / "hooks",
)
SUFFIXES = (".py", ".html", ".js", ".css")
RECORD = ".server.code"


def code_stamp(code_dirs=CODE_DIRS):
    return max(
        (p.stat().st_mtime_ns for d in code_dirs for p in d.rglob("*") if p.suffix in SUFFIXES),
        default=0,
    )


def record(folder: Path, pid: int, code_dirs=CODE_DIRS) -> None:
    loaded = {"pid": pid, "stamp": code_stamp(code_dirs), "dirs": [str(d) for d in code_dirs]}
    (folder / RECORD).write_text(json.dumps(loaded))


def loaded(folder: Path) -> dict | None:
    try:
        held = json.loads((folder / RECORD).read_text())
    except (OSError, ValueError):
        return None
    return held if isinstance(held, dict) else None


def stale(folder: Path, pid: int) -> bool:
    held = loaded(folder)
    if held is None or held.get("pid") != pid:
        return True
    return code_stamp(tuple(Path(d) for d in held.get("dirs", ()))) != held.get("stamp")
