from collections.abc import Callable
from dataclasses import dataclass

import orjson

from .rows import encode


@dataclass(frozen=True, slots=True)
class Fragment:
    value: object
    text: str | None
    parts: dict | tuple | None = None
    signature: str | None = None


def encoded(fragment: Fragment) -> str:
    if fragment.text is not None:
        return fragment.text
    if isinstance(fragment.parts, tuple):
        return "[" + ",".join(encoded(item) for item in fragment.parts) + "]"
    return "{" + ",".join(encode(key) + ":" + encoded(item) for key, item in fragment.parts.items()) + "}"


def leaf(value: object, previous: Fragment | None = None) -> Fragment:
    try:
        signature = orjson.dumps(value).decode()
    except orjson.JSONEncodeError:
        signature = encode(value)
    if previous is not None and value == previous.value and signature == previous.signature:
        return previous
    text = encode(value)
    return Fragment(value, text, signature=text if signature == text else signature)


def sequence(value: list, previous: Fragment | None = None) -> Fragment:
    old = previous.parts if previous is not None and isinstance(previous.parts, tuple) else None
    known = old or ()
    parts = tuple(leaf(item, known[index] if index < len(known) else None) for index, item in enumerate(value))
    if old is not None and len(parts) == len(old) and all(new is kept for new, kept in zip(parts, old)):
        return previous
    return Fragment([item.value for item in parts], None, parts)


def mapping(
    value: dict, previous: Fragment | None, make: Callable[[str, object, Fragment | None], Fragment]
) -> Fragment:
    old = previous.parts if previous is not None and isinstance(previous.parts, dict) else None
    known = old or {}
    parts = {key: make(key, item, known.get(key)) for key, item in value.items()}
    if old is not None and list(parts) == list(old) and all(parts[key] is old[key] for key in parts):
        return previous
    return Fragment({key: item.value for key, item in parts.items()}, None, parts)


def meta_part(key: str, value: object, previous: Fragment | None) -> Fragment:
    if key == "events" and isinstance(value, list):
        return sequence(value, previous)
    if key == "stamps" and isinstance(value, dict):
        return mapping(value, previous, lambda key, item, old: leaf(item, old))
    return leaf(value, previous)


def document_part(key: str, value: object, previous: Fragment | None) -> Fragment:
    if key == "_meta" and isinstance(value, dict):
        return mapping(value, previous, meta_part)
    if isinstance(value, list):
        return sequence(value, previous)
    return leaf(value, previous)


def document(state: dict, previous: Fragment | None = None) -> Fragment:
    return mapping(state, previous, document_part)
