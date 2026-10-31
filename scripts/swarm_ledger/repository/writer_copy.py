from . import refresh
from .entry import thawed
from .events import read_events
from .fragments import Fragment, document


def written(state: dict, previous: Fragment | None) -> tuple[Fragment, dict]:
    """The stored fragments of state and the writer copy the next write takes; stamp records are replaced, never
    changed in place, so the stamps fragment keeps sharing them and is not detached record by record."""
    fragment = document(state, previous)
    meta = fragment.parts.get("_meta")
    stamps = meta.parts.get("stamps") if meta is not None and isinstance(meta.parts, dict) else None
    shared = stamps if isinstance(stamps, Fragment) and isinstance(stamps.parts, dict) else None
    fragment, writer = detached(fragment, previous, state, shared)
    if shared is not None:
        writer["_meta"]["stamps"] = dict(shared.value)
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


def detached(
    fragment: Fragment, previous: Fragment | None, value: object, shared: Fragment | None = None
) -> tuple[Fragment, object]:
    if fragment is previous or fragment is shared:
        return fragment, value
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
