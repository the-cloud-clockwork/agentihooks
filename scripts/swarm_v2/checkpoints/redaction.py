"""Secret redaction for native conversation files: each match becomes a marker and only the count is kept."""

import json
import re

from hooks import secrets

FIELD = "secret_redactions"
MODE = "strict"
MARK = "[REDACTED:"
MARKER = re.compile(r"\[REDACTED:\w+\]")
OPENING = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
CLOSING = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
UNREDACTABLE = "secret_unredactable"
CODEC_ERRORS = "surrogateescape"


class Unredactable(ValueError):
    pass


def _redact(text: str) -> str:
    """A key whose end lies in another record cannot be hidden from here, so the material is refused."""
    if len(OPENING.findall(text)) > len(CLOSING.findall(text)):
        raise Unredactable(UNREDACTABLE)
    return secrets.redact(text, mode=MODE)


def _count(before: str, after: str) -> int:
    return after.count(MARK) - before.count(MARK)


def _plain(text: str, found: list[int]) -> str:
    redacted = _redact(text)
    found.append(_count(text, redacted))
    return redacted


def _nested(path: tuple[str, ...], text: str) -> str:
    fragment: object = text
    for key in reversed(path):
        fragment = {key: fragment}
    return json.dumps(fragment)


def _value(text: str, path: tuple[str, ...], found: list[int]) -> str:
    """A match that needs the keys above the value hides the whole value."""
    own = _redact(text)
    fragment = _nested(path, own)
    keyed = _redact(fragment)
    if keyed != fragment:
        own = " ".join(MARKER.findall(keyed))
    found.append(_count(text, own))
    return own


def _fields(record: dict, path: tuple[str, ...], found: list[int]) -> dict:
    safe = {name: _plain(name, found) for name in record}
    return {safe[name]: _redacted(item, (*path, safe[name]), found) for name, item in record.items()}


def _redacted(value: object, path: tuple[str, ...], found: list[int]) -> object:
    if isinstance(value, dict):
        return _fields(value, path, found)
    if isinstance(value, list):
        return [_redacted(item, path, found) for item in value]
    return _value(value, path, found) if isinstance(value, str) else value


def _rewritten(line: str, record: object, found: list[int]) -> str:
    cleaned = _redacted(record, (), found)
    if cleaned == record:
        return line
    return json.dumps(cleaned, separators=(",", ":")) + line[len(line.rstrip()) :]


def _record(line: str) -> object:
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, (dict, list, str)) else None


def scrub(data: bytes) -> tuple[bytes, int]:
    """JSON object, array and string lines are redacted value by value; each run of other lines is redacted as one
    text. The count is the number of markers placed."""
    found: list[int] = []
    out: list[str] = []
    block: list[str] = []
    *whole, last = data.decode(errors=CODEC_ERRORS).split("\n")
    for line in [*(text + "\n" for text in whole), last]:
        record = _record(line)
        if record is None:
            block.append(line)
            continue
        out += [_plain("".join(block), found), _rewritten(line, record, found)]
        block = []
    out.append(_plain("".join(block), found))
    return "".join(out).encode(errors=CODEC_ERRORS), sum(found)
