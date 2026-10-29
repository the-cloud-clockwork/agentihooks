import os
import re
import shlex
import sys
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path

from scripts import account_names

START = "# ===AGENTIHOOKS-AICODING-TOKENS==="
END = "# ===AGENTIHOOKS-AICODING-TOKENS-END==="
REFRESH_FLAG = "AGENTIHOOKS_AICODE_TOKEN_REFRESH"
TOKEN_EXPORTS = ("AH_CC_TOKEN_", "AH_CX_TOKEN_")
RESERVED = ("api", "default", "help", "status")
SLUG_RULE = (
    "slug is lowercased, '@' becomes 'at' and every other character outside a-z and 0-9 becomes '_'; "
    f"the result must not start with '_' and must not be {', '.join(RESERVED)}"
)
SLUG_NOTE = (
    "Note: use the email of the target account as the slug, so each token is recognisable at a glance "
    "(nestor.colt@gmail.com becomes {prefix}nestor_coltatgmail_com)."
)
_SLUG = re.compile(r"[a-z0-9][a-z0-9_]*")
_EXPORT = re.compile(r"\s*export\s+([^=\s]+)=")
_write = os.write


class SlugError(ValueError):
    pass


def slug(raw: str) -> str:
    found = account_names.secret_key(raw).replace("-", "_")
    if not _SLUG.fullmatch(found) or found in RESERVED:
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


def token_exports(text: str) -> dict[str, str]:
    lines = text.splitlines()
    found = {}
    for start, end in _blocks(lines):
        for line in lines[start + 1 : end]:
            try:
                words = shlex.split(line)
            except ValueError:
                continue
            if len(words) == 2 and words[0] == "export":
                name, _, value = words[1].partition("=")
                if value and name.startswith(TOKEN_EXPORTS):
                    found.setdefault(name, value)
    return found


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


def remove_exports(path: Path, names: Iterable[str]) -> bool:
    if not path.exists():
        return False
    path = path.resolve()
    text = path.read_bytes().decode()
    lines = text.splitlines(keepends=True)
    for start, end in reversed(_blocks(lines)):
        lines = lines[: start + 1] + _drop(lines[start + 1 : end], names) + lines[end:]
    if "".join(lines) == text:
        return False
    atomic_write(path, "".join(lines).encode(), path.stat().st_mode & 0o777)
    return True
