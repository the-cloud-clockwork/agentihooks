"""Secret redaction for native conversation files: each match becomes a marker and only the count is kept."""

import json
import re

from hooks import secrets

FIELD = "secret_redactions"
MODE = "strict"
MARK = "[REDACTED:"
MARKER = re.compile(r"\[REDACTED:\w+\]")
LINE = re.compile(r"[^\n]*\n|[^\n]+")
CODEC_ERRORS = "surrogateescape"


def _count(before: str, after: str) -> int:
    return after.count(MARK) - before.count(MARK)


def _plain(text: str, found: list[int]) -> str:
    redacted = secrets.redact(text, mode=MODE)
    found.append(_count(text, redacted))
    return redacted


def _value(text: str, key: str, found: list[int]) -> str:
    """A match that needs the record key hides the whole value."""
    own = secrets.redact(text, mode=MODE)
    fragment = json.dumps({key: text})
    keyed = secrets.redact(fragment, mode=MODE)
    if _count(fragment, keyed) > _count(text, own):
        own = " ".join(MARKER.findall(keyed))
    found.append(_count(text, own))
    return own


def _redacted(value: object, key: str, found: list[int]) -> object:
    if isinstance(value, dict):
        safe = {name: _value(name, "", found) for name in value}
        return {safe[name]: _redacted(item, safe[name], found) for name, item in value.items()}
    if isinstance(value, list):
        return [_redacted(item, key, found) for item in value]
    return _value(value, key, found) if isinstance(value, str) else value


def _rewritten(line: str, record: object, found: list[int]) -> str:
    cleaned = _redacted(record, "", found)
    if cleaned == record:
        return line
    body = line.rstrip("\r\n")
    return json.dumps(cleaned, separators=(",", ":")) + line[len(body) :]


def scrub(data: bytes) -> tuple[bytes, int]:
    """JSON lines are redacted field by field; each run of other lines is redacted as one text."""
    found: list[int] = []
    out: list[str] = []
    block: list[str] = []
    for line in LINE.findall(data.decode(errors=CODEC_ERRORS)):
        try:
            record = json.loads(line)
        except ValueError:
            block.append(line)
            continue
        out += [_plain("".join(block), found), _rewritten(line, record, found)]
        block = []
    out.append(_plain("".join(block), found))
    return "".join(out).encode(errors=CODEC_ERRORS), sum(found)
