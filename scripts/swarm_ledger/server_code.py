"""The ledger server's code folders and the code stamp it loaded."""

import json
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
ROOT = CODE_DIR.parents[1]
CODE_DIRS = (
    CODE_DIR,
    *(ROOT / "scripts" / name for name in ("inbox", "swarm", "swarm_v2", "handoff", "doctor", "gates", "hive")),
    ROOT / "hooks",
)
SUFFIXES = (".py", ".html", ".js", ".css")
RECORD = ".server.code"


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def code_stamp(code_dirs: tuple[Path, ...] = CODE_DIRS) -> int:
    return max((_mtime(p) for d in code_dirs for p in d.rglob("*") if p.suffix in SUFFIXES), default=0)


def record(folder: Path, pid: int, stamp: int, code_dirs: tuple[Path, ...] = CODE_DIRS) -> None:
    written = folder / f"{RECORD}.{pid}"
    written.write_text(json.dumps({"pid": pid, "stamp": stamp, "dirs": [str(d) for d in code_dirs]}))
    written.replace(folder / RECORD)


def loaded(folder: Path) -> dict | None:
    try:
        held = json.loads((folder / RECORD).read_text())
    except (OSError, ValueError):
        return None
    return held if isinstance(held, dict) else None


def recorded(folder: Path, pid: int) -> bool:
    held = loaded(folder)
    return held is not None and held.get("pid") == pid


def stale(folder: Path, pid: int) -> bool:
    held = loaded(folder)
    if held is None or held.get("pid") != pid:
        return True
    return code_stamp(tuple(Path(d) for d in held.get("dirs", ()))) != held.get("stamp")
