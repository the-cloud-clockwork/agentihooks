"""Where each rendered rule and doctrine file came from: one trace row per file, written beside the render.

SessionStart records these rows into the session's injection trace, so a
correction can name a rule or doctrine file by its repo, path and blob.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def path(name: str, target: str, root: Path) -> Path:
    return root / name / f"{target}.sources.json"


def blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _repo(file: Path) -> Path | None:
    return next((parent for parent in file.parents if (parent / ".git").exists()), None)


def _place(file: Path) -> tuple[Path | None, str]:
    repo = _repo(file)
    return repo, file.relative_to(repo).as_posix() if repo else str(file)


def _name(repo: Path | None, rel: str) -> str:
    return f"{repo.name}/{rel}" if repo else rel


def source(file: Path) -> str:
    return _name(*_place(file.resolve()))


def row(layer: str, file: Path) -> dict:
    file = file.resolve()
    data = file.read_bytes()
    repo, rel = _place(file)
    text = next((line.strip() for line in data.decode(errors="replace").splitlines() if line.strip()), "")
    return {
        "layer": layer,
        "source": _name(repo, rel),
        "locator": {"repo": str(repo or ""), "path": rel, "blob": blob(data)},
        "text": text,
    }


def doctrine_files(bundle: Path | None, dirs: list[tuple[str, Path]]) -> list[Path]:
    from scripts.profiles import manifestos

    files = []
    if bundle is not None:
        shared = bundle / ".claude" / "CLAUDE.md"
        files.append(shared if shared.exists() else bundle / "CLAUDE.md")
    files += [directory / "CLAUDE.md" for _, directory in dirs]
    files += manifestos.paths(bundle, dirs)
    return [file for file in files if file.is_file() and file.read_text().strip()]


def rows(bundle: Path | None, dirs: list[tuple[str, Path]], rules: dict[str, Path]) -> list[dict]:
    return [row("doctrine", file) for file in doctrine_files(bundle, dirs)] + [
        row("rule", file) for file in rules.values()
    ]


def write(dst: Path, found: list[dict]) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(found) + "\n")
