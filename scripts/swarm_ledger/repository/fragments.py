from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import compress, islice
from operator import is_not
from typing import Any, cast

import orjson

from .rows import encode
from .tracked import held

RECORD_MAPS = frozenset({"members", "api_operations"})


@dataclass(frozen=True, slots=True)
class Fragment:
    value: object
    parts: dict | tuple | None = None
    signature: int | None = None
    data: bytes | None = field(default=None, compare=False)


def encoded(fragment: Fragment) -> str:
    if fragment.parts is None:
        return encode(fragment.value)
    if isinstance(fragment.parts, tuple):
        return "[" + ",".join(encoded(item) for item in fragment.parts) + "]"
    return "{" + ",".join(encode(key) + ":" + encoded(item) for key, item in fragment.parts.items()) + "}"


def exported(fragment: Fragment) -> bytes:
    """The UTF-8 text of fragment, kept on it so exports of later generations that share it only join bytes."""
    if fragment.data is None:
        if fragment.parts is None:
            data = encode(fragment.value).encode()
        elif isinstance(fragment.parts, tuple):
            data = b"[" + b",".join(exported(item) for item in fragment.parts) + b"]"
        else:
            data = members(fragment.parts)
        object.__setattr__(fragment, "data", data)
    return fragment.data


def members(parts: dict) -> bytes:
    return b"{" + b",".join(encode(key).encode() + b":" + exported(item) for key, item in parts.items()) + b"}"


def leaf(value: object, previous: Fragment | None = None) -> Fragment:
    if previous is not None and value is previous.value:
        return previous
    kept = held(value)
    if kept is not None:
        return kept
    try:
        signature = hash(orjson.dumps(value))
    except orjson.JSONEncodeError:
        signature = hash(encode(value))
    if previous is not None and value == previous.value and signature == previous.signature:
        return previous
    encode(value)  # rejects NaN and Infinity, which orjson writes as null
    return Fragment(value, signature=signature)


def sequence(value: list, previous: Fragment | None = None) -> Fragment:
    old = previous.parts if previous is not None and isinstance(previous.parts, tuple) else None
    known = old or ()
    parts = tuple(leaf(item, known[index] if index < len(known) else None) for index, item in enumerate(value))
    if old is not None and len(parts) == len(old) and all(new is kept for new, kept in zip(parts, old)):
        return previous
    return Fragment([item.value for item in parts], parts)


def mapping(
    value: dict, previous: Fragment | None, make: Callable[[str, object, Fragment | None], Fragment]
) -> Fragment:
    old = previous.parts if previous is not None and isinstance(previous.parts, dict) else None
    known = old or {}
    parts = {key: make(key, item, known.get(key)) for key, item in value.items()}
    if old is not None and list(parts) == list(old) and all(parts[key] is old[key] for key in parts):
        return previous
    return Fragment({key: item.value for key, item in parts.items()}, parts)


def stamp(key: str, value: object, previous: Fragment | None) -> Fragment:
    """A stamp record is replaced, never changed in place, so the writer copy's shared record is still the one stored."""
    if previous is not None and value is previous.value:
        return previous
    return leaf(value, previous)


def replaced(new: dict[Any, Any], old: dict[Any, Any]) -> list[Any] | None:
    """The keys of new whose value is not the object old holds at that key, then the keys new adds after old's, in
    order; None when new does not start with old's keys in old's order. The scan runs in C."""
    if list(islice(new, len(old))) != list(old):
        return None
    return [*compress(old, map(is_not, new.values(), old.values())), *islice(new, len(old), None)]


def stamps(value: dict[Any, Any], previous: Fragment | None) -> Fragment:
    """The stamps mapping as ``mapping`` with ``stamp`` builds it, encoding only the records a write replaced or added
    when the stored keys keep their order."""
    if previous is None:
        return mapping(value, previous, stamp)
    held: object = previous.parts  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    kept: object = previous.value
    if not isinstance(held, dict) or not isinstance(kept, dict):
        return mapping(value, previous, stamp)
    old, stored = cast(dict[str, Fragment], held), cast(dict[str, object], kept)
    changed = replaced(value, stored)
    if changed is None:
        return mapping(value, previous, stamp)
    parts, values, fresh = dict(old), dict(stored), False
    for key in changed:
        part = leaf(value[key], old.get(key))
        fresh = fresh or part is not old.get(key)
        parts[key], values[key] = part, part.value
    return Fragment(values, parts) if fresh else previous


def record(key: str, value: object, previous: Fragment | None) -> Fragment:
    """One record of a metadata map a write changes a record at a time, encoded alone so the write copies only it."""
    return leaf(value, previous)


def meta_part(key: str, value: object, previous: Fragment | None) -> Fragment:
    if key == "events" and isinstance(value, list):
        return sequence(value, previous)
    if key == "stamps" and isinstance(value, dict):
        return stamps(cast(dict[str, object], value), previous)
    if key in RECORD_MAPS and isinstance(value, dict):
        return mapping(cast(dict[str, object], value), previous, record)
    return leaf(value, previous)


def document_part(key: str, value: object, previous: Fragment | None) -> Fragment:
    if key == "_meta" and isinstance(value, dict):
        return mapping(value, previous, meta_part)
    if isinstance(value, list):
        return sequence(value, previous)
    return leaf(value, previous)


def document(state: dict, previous: Fragment | None = None) -> Fragment:
    return mapping(state, previous, document_part)
