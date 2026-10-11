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
RESERVED = frozenset({"status", "help", "default", "api"})
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]*")
_EXPORT = re.compile(r"\s*export\s+([^=\s]+)=")


class SlugError(ValueError):
    pass


def slug(raw: str) -> str:
    found = raw.replace("-", "_")
    if not _SLUG.fullmatch(found) or found.lower() in RESERVED:
        reserved = ", ".join(sorted(RESERVED))
        raise SlugError(f"slug must be letters, digits, '_' or '-', not start with '_' and not be {reserved}: {raw!r}")
    return found


def rc_file(platform: str | None = None) -> Path:
    return Path.home() / (".zshrc" if (platform or sys.platform) == "darwin" else ".bashrc")


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
    return bool(_blocks(text.splitlines(keepends=True)))


def _exported(line: str) -> str:
    found = _EXPORT.match(line)
    return found.group(1) if found else ""


def _rewrite(lines: list[str], span: tuple[int, int], name: str, line: str, stale: Iterable[str]) -> list[str]:
    start, end = span
    dropped = {name, *stale}
    body, placed = [], not line
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
    """The first block holds each export; later duplicate blocks lose it so they cannot override it."""
    lines = text.splitlines(keepends=True)
    if not _blocks(lines):
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines += [f"{START}\n", f"{END}\n"]
    stale = tuple(stale)
    for name, value in entries.items():
        first, *later = _blocks(lines)
        for span in reversed(later):
            lines = _rewrite(lines, span, name, "", stale)
        lines = _rewrite(lines, first, name, f"export {name}={shlex.quote(value)}\n", stale)
    return "".join(lines)


def write_rc(path: Path, entries: Mapping[str, str], stale: Iterable[str] = ()) -> None:
    path = path.resolve()
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
