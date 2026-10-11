"""The managed block of agent token exports in the operator's shell rc file, for any agent harness."""

import os
import re
import shlex
import sys
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path

START = "# ===AGENTIHOOKS-AICODING-TOKENS==="
END = "# ===AGENTIHOOKS-AICODING-TOKENS-END==="
RESERVED = frozenset({"status", "help", "default", "api"})
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]*")
_EXPORT = re.compile(r"\s*export\s+([^=\s]+)=")


class SlugError(ValueError):
    pass


def slug(raw: str) -> str:
    found = raw.replace("-", "_")
    if not _SLUG.fullmatch(found) or found in RESERVED:
        reserved = ", ".join(sorted(RESERVED))
        raise SlugError(f"slug must be letters, digits, '_' or '-', not start with '_' and not be {reserved}: {raw!r}")
    return found


def rc_file(platform: str | None = None) -> Path:
    return Path.home() / (".zshrc" if (platform or sys.platform) == "darwin" else ".bashrc")


def _block(lines: list[str]) -> tuple[int, int] | None:
    start = next((index for index, line in enumerate(lines) if line.rstrip() == START), None)
    if start is None:
        return None
    end = next((index for index in range(start + 1, len(lines)) if lines[index].rstrip() == END), None)
    return None if end is None else (start, end)


def has_block(text: str) -> bool:
    return _block(text.splitlines(keepends=True)) is not None


def _exported(line: str) -> str:
    found = _EXPORT.match(line)
    return found.group(1) if found else ""


def _upsert_one(lines: list[str], span: tuple[int, int], name: str, value: str, stale: Iterable[str]) -> list[str]:
    start, end = span
    line = f"export {name}={shlex.quote(value)}\n"
    dropped = {name, *stale}
    body, placed = [], False
    for item in lines[start + 1 : end]:
        found = _exported(item)
        if found == name and not placed:
            body.append(line)
            placed = True
        elif found not in dropped:
            body.append(item)
    if not placed:
        body.append(line)
    return lines[: start + 1] + body + lines[end:]


def upsert(text: str, entries: Mapping[str, str], stale: Iterable[str] = ()) -> str:
    lines = text.splitlines(keepends=True)
    if _block(lines) is None:
        sep = "\n" if text and not text.endswith("\n") else ""
        lines = [*lines[:-1], lines[-1] + sep] if lines else []
        lines += [f"{START}\n", f"{END}\n"]
    stale = tuple(stale)
    for name, value in entries.items():
        lines = _upsert_one(lines, _block(lines), name, value, stale)
    return "".join(lines)


def write_rc(path: Path, entries: Mapping[str, str], stale: Iterable[str] = ()) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(upsert(text, entries, stale))
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update_existing(path: Path, entries: Mapping[str, str]) -> bool:
    if not path.exists() or not has_block(path.read_text(encoding="utf-8")):
        return False
    write_rc(path, entries)
    return True
