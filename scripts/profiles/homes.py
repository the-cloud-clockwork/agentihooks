import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from scripts.profiles import binding

HOMES, CURRENT = ".homes", "current"
GRACE_SECONDS = 600
PROC = Path("/proc")


def live_homes(proc: Path = PROC) -> list[Path]:
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "environ").read_bytes()
        except OSError:
            continue
        for item in raw.split(b"\0"):
            key, _, value = item.partition(b"=")
            if key.decode(errors="replace") in binding.HOMES.values() and value:
                found.append(Path(value.decode(errors="replace")).resolve())
    return found


def _homes(root: Path, name: str) -> Path:
    return root / HOMES / name


def current(root: Path, name: str) -> Path | None:
    pointer = _homes(root, name) / CURRENT
    if pointer.is_dir():
        return pointer.resolve()
    legacy = root / name
    return legacy if legacy.is_dir() and not legacy.is_symlink() else None


def owner(root: Path, home: Path) -> str | None:
    try:
        parts = home.resolve().relative_to(root.resolve()).parts
    except ValueError:
        return None
    if len(parts) == 2:
        return parts[0]
    return parts[1] if len(parts) == 4 and parts[0] == HOMES else None


def fresh(root: Path, name: str, stamp: dict) -> Path:
    homes = _homes(root, name)
    homes.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(json.dumps(stamp, sort_keys=True).encode()).hexdigest()[:12]
    return Path(tempfile.mkdtemp(prefix=f"{digest}-", dir=homes))


def promote(root: Path, name: str, home: Path) -> None:
    pointer = _homes(root, name) / CURRENT
    staged = pointer.with_name(f".{CURRENT}-{os.getpid()}")
    staged.unlink(missing_ok=True)
    staged.symlink_to(home.name)
    staged.replace(pointer)
    collect(root, name)
    named = root / name
    if not named.exists() and not named.is_symlink():
        named.symlink_to(Path(HOMES) / name / CURRENT)


def collect(root: Path, name: str) -> None:
    homes = _homes(root, name)
    kept = (homes / CURRENT).resolve()
    old = [p for p in homes.iterdir() if p.is_dir() and not p.is_symlink() and p.resolve() != kept]
    legacy = root / name
    if legacy.is_dir() and not legacy.is_symlink():
        old.append(legacy)
    live = live_homes()
    for home in old:
        # A launch reads its home before its harness process exists to hold it.
        if time.time() - home.stat().st_mtime < GRACE_SECONDS:
            continue
        if not any(path.is_relative_to(home.resolve()) for path in live):
            shutil.rmtree(home)
