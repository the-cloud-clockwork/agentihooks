from typing import cast

from . import refresh, tracked
from .entry import thawed
from .events import read_events
from .fragments import Fragment, document, replaced


def written(state: dict, previous: Fragment | None) -> tuple[Fragment, dict]:
    fragment, writer = detached(document(state, previous), previous, state)
    meta = fragment.parts.get("_meta")
    stamps = meta.parts.get("stamps") if meta is not None and isinstance(meta.parts, dict) else None
    if stamps is not None and isinstance(stamps.parts, dict):
        writer["_meta"]["stamps"] = dict(stamps.value)
    return fragment, writer


def refreshed(connection, slug: str, cached, paths: set) -> tuple[dict | None, dict | None]:
    state = refresh.refreshed(connection, slug, cached.state, paths)
    writer, cached.writer = cached.writer, None
    if state is None or writer is None:
        return state, None
    writer = refresh.refreshed(connection, slug, writer, paths)
    if writer is not None and "events" in writer["_meta"]:
        writer["_meta"]["events"] = read_events(connection, slug)
    return state, writer


def detached(fragment: Fragment, previous: Fragment | None, value: object) -> tuple[Fragment, object]:
    if fragment is previous:
        return fragment, value
    if fragment.parts is None:
        if tracked.held(value) is fragment:
            return fragment, value
        stored = Fragment(thawed(fragment.value), signature=fragment.signature)
        return stored, tracked.bound(stored)
    old = previous.parts if previous is not None and type(previous.parts) is type(fragment.parts) else None
    if isinstance(fragment.parts, dict):
        own = cast(dict[str, Fragment], fragment.parts)
        known = cast(dict[str, Fragment], old or {})
        held = cast(dict[str, object], value)
        changed = replaced(own, known)
        parts, values, writer = dict(own), dict(cast(dict[str, object], fragment.value)), dict(held)
        for key in list(own) if changed is None else changed:
            part, writer[key] = detached(parts[key], known.get(key), held[key])
            parts[key], values[key] = part, part.value
        return Fragment(values, parts), writer
    known = old or ()
    pairs = [
        detached(part, known[index] if index < len(known) else None, item)
        for index, (part, item) in enumerate(zip(fragment.parts, value))
    ]
    parts = tuple(pair[0] for pair in pairs)
    return Fragment([part.value for part in parts], parts), [pair[1] for pair in pairs]
