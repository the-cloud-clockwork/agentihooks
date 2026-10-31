from typing import Any, cast

from . import refresh
from .entry import thawed
from .events import read_events
from .fragments import Fragment, document


def written(state: dict[str, Any], previous: Fragment | None) -> tuple[Fragment, dict[str, Any]]:
    """The stored fragments of state and the writer copy the next write takes; the stamps mapping is detached in one
    pass that copies apart only the records the previous fragments do not hold."""
    fragment = document(state, previous)
    fragment, writer = detached(fragment, previous, state, stamps_of(fragment))
    copy = cast("dict[str, Any]", writer)
    stamps = stamps_of(fragment)
    if stamps is not None:
        copy["_meta"]["stamps"] = dict(cast("dict[str, object]", stamps.value))
    return fragment, copy


def stamps_of(fragment: Fragment) -> Fragment | None:
    """The stamps mapping fragment of a document fragment, or None when it holds no stamps mapping."""
    meta = cast("dict[str, object]", fragment.parts).get("_meta") if isinstance(fragment.parts, dict) else None
    if not isinstance(meta, Fragment) or not isinstance(meta.parts, dict):
        return None
    stamps = cast("dict[str, object]", meta.parts).get("stamps")
    return stamps if isinstance(stamps, Fragment) and isinstance(stamps.parts, dict) else None


def refreshed(connection, slug: str, cached, paths: set) -> tuple[dict | None, dict | None]:
    state = refresh.refreshed(connection, slug, cached.state, paths)
    writer, cached.writer = cached.writer, None
    if state is None or writer is None:
        return state, None
    writer = refresh.refreshed(connection, slug, writer, paths)
    if writer is not None and "events" in writer["_meta"]:
        writer["_meta"]["events"] = read_events(connection, slug)
    return state, writer


def records_apart(fragment: Fragment, previous: Fragment | None) -> Fragment:
    """The stamps mapping with every record fragment previous does not hold copied apart, as detached copies leaves."""
    parts = cast("dict[str, Fragment]", fragment.parts)
    known = (
        cast("dict[str, Fragment]", previous.parts) if previous is not None and isinstance(previous.parts, dict) else {}
    )
    fresh = {
        key: Fragment(thawed(part.value), signature=part.signature)
        for key, part in parts.items()
        if part is not known.get(key)
    }
    merged, values = dict(parts), dict(cast("dict[str, object]", fragment.value))
    merged.update(fresh)
    values.update({key: part.value for key, part in fresh.items()})
    return Fragment(values, merged)


def detached(
    fragment: Fragment, previous: Fragment | None, value: object, shared: Fragment | None = None
) -> tuple[Fragment, object]:
    if fragment is previous:
        return fragment, value
    if fragment is shared:
        return records_apart(fragment, previous), value
    if fragment.parts is None:
        return Fragment(thawed(fragment.value), signature=fragment.signature), thawed(fragment.value)
    old = previous.parts if previous is not None and type(previous.parts) is type(fragment.parts) else None
    if isinstance(fragment.parts, dict):
        known = old or {}
        pairs = {key: detached(part, known.get(key), value[key], shared) for key, part in fragment.parts.items()}
        parts = {key: pair[0] for key, pair in pairs.items()}
        return Fragment({key: part.value for key, part in parts.items()}, parts), {
            key: pair[1] for key, pair in pairs.items()
        }
    known = old or ()
    pairs = [
        detached(part, known[index] if index < len(known) else None, item, shared)
        for index, (part, item) in enumerate(zip(fragment.parts, value))
    ]
    parts = tuple(pair[0] for pair in pairs)
    return Fragment([part.value for part in parts], parts), [pair[1] for pair in pairs]
