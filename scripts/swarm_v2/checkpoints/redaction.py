"""Secret redaction for native conversation files: each match becomes a marker and only the count is kept."""

import json
import re

from hooks import secrets

FIELD = "secret_redactions"
MODE = "strict"
MARK = "[REDACTED:"
MARKER = re.compile(r"\[REDACTED:\w+\]")
LINE = re.compile(r"[^\n]*\n|[^\n]+")
END = re.compile(r"[\r\n]*\Z")
CODEC_ERRORS = "surrogateescape"


def _redact(text: str) -> str:
    return secrets.redact(text, mode=MODE)


def _count(before: str, after: str) -> int:
    return after.count(MARK) - before.count(MARK)


def _plain(text: str, found: list[int]) -> str:
    redacted = _redact(text)
    found.append(_count(text, redacted))
    return redacted


def _value(text: str, key: str, found: list[int]) -> str:
    """A match that needs the record key hides the whole value."""
    own = _redact(text)
    fragment = json.dumps({key: text})
    keyed = _redact(fragment)
    if set(MARKER.findall(keyed)) - set(MARKER.findall(own)) - set(MARKER.findall(fragment)):
        own = " ".join(MARKER.findall(keyed))
    found.append(_count(text, own))
    return own


def _fields(record: dict, found: list[int]) -> dict:
    safe = {name: _plain(name, found) for name in record}
    return {safe[name]: _redacted(item, safe[name], found) for name, item in record.items()}


def _redacted(value: object, key: str, found: list[int]) -> object:
    if isinstance(value, dict):
        return _fields(value, found)
    if isinstance(value, list):
        return [_redacted(item, key, found) for item in value]
    return _value(value, key, found) if isinstance(value, str) else value


def _rewritten(line: str, record: dict, found: list[int]) -> str:
    cleaned = _fields(record, found)
    if cleaned == record:
        return line
    return json.dumps(cleaned, separators=(",", ":")) + END.search(line).group()


def _record(line: str) -> dict | None:
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def scrub(data: bytes) -> tuple[bytes, int]:
    """JSON object lines are redacted field by field; each run of other lines is redacted as one text. The count is
    the number of markers placed."""
    found: list[int] = []
    out: list[str] = []
    block: list[str] = []
    for line in LINE.findall(data.decode(errors=CODEC_ERRORS)):
        record = _record(line)
        if record is None:
            block.append(line)
            continue
        out += [_plain("".join(block), found), _rewritten(line, record, found)]
        block = []
    out.append(_plain("".join(block), found))
    return "".join(out).encode(errors=CODEC_ERRORS), sum(found)
