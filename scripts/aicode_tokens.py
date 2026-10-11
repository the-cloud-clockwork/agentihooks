import os
import re
import shlex
import sys
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path

START = "# ===AGENTIHOOKS-AICODING-TOKENS==="
END = "# ===AGENTIHOOKS-AICODING-TOKENS-END==="
REFRESH_FLAG = "AGENTIHOOKS_AICODE_TOKEN_REFRESH"
RESERVED = ("api", "default", "help", "status")
SLUG_RULE = f"slug must be letters, digits, '_' or '-', not start with '_' and not be {', '.join(RESERVED)}"
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]*")
_EXPORT = re.compile(r"\s*export\s+([^=\s]+)=")
_write = os.write


class SlugError(ValueError):
    pass


def slug(raw: str) -> str:
    found = raw.replace("-", "_")
    if not _SLUG.fullmatch(found) or found.lower() in RESERVED:
        raise SlugError(f"{SLUG_RULE}: {raw!r}")
    return found


def refresh_authority(environ: Mapping[str, str]) -> bool:
    return str(environ.get(REFRESH_FLAG)).strip().lower() == "true"


def rc_file(platform: str | None = None) -> Path:
    return Path.home() / (".zshrc" if (platform or sys.platform) == "darwin" else ".bashrc")


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        try:
            os.fchmod(fd, mode)
            if _write(fd, data) != len(data):
                raise OSError(f"short write to {tmp}")
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _blocks(lines: list[str]) -> list[tuple[int, int]]:
    spans, start = [], None
    for index, line in enumerate(lines):
        marker = line.rstrip()
        if marker == START:
            start = index
        elif marker == END and start is not None:
            spans.append((start, index))
            start = None
    return spans


def has_block(text: str) -> bool:
    return bool(_blocks(text.splitlines()))


def _exported(line: str) -> str | None:
    found = _EXPORT.match(line)
    return found and found.group(1)


def _drop(body: list[str], names: Iterable[str]) -> list[str]:
    names = set(names)
    return [item for item in body if _exported(item) not in names]


def _place(body: list[str], name: str, line: str, stale: Iterable[str]) -> list[str]:
    found = [_exported(item) for item in body]
    if name not in found:
        return [*_drop(body, stale), line]
    at = found.index(name)
    dropped = {name, *stale}
    return [
        line if index == at else item for index, item in enumerate(body) if index == at or found[index] not in dropped
    ]


def upsert(text: str, entries: Mapping[str, str], stale: Iterable[str] = ()) -> str:
    lines = text.splitlines(keepends=True)
    if not _blocks(lines):
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines += [f"{START}\n", f"{END}\n"]
    stale = tuple(stale)
    for name, value in entries.items():
        (start, end), *later = _blocks(lines)
        for inner, outer in reversed(later):
            lines = lines[: inner + 1] + _drop(lines[inner + 1 : outer], [name, *stale]) + lines[outer:]
        body = _place(lines[start + 1 : end], name, f"export {name}={shlex.quote(value)}\n", stale)
        lines = lines[: start + 1] + body + lines[end:]
    return "".join(lines)


def write_rc(path: Path, entries: Mapping[str, str], stale: Iterable[str] = ()) -> None:
    path = path.resolve()
    exists = path.exists()
    text = path.read_bytes().decode() if exists else ""
    atomic_write(path, upsert(text, entries, stale).encode(), path.stat().st_mode & 0o777 if exists else 0o600)


def update_existing(path: Path, entries: Mapping[str, str]) -> bool:
    if not path.exists() or not has_block(path.read_bytes().decode()):
        return False
    write_rc(path, entries)
    return True
