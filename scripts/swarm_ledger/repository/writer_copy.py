from .entry import thawed
from .fragments import Fragment, document


def written(state: dict, previous: Fragment | None) -> tuple[Fragment, dict]:
    return detached(document(state, previous), previous, state)


def detached(fragment: Fragment, previous: Fragment | None, value: object) -> tuple[Fragment, object]:
    if fragment is previous:
        return fragment, value
    if fragment.parts is None:
        return Fragment(thawed(fragment.value), signature=fragment.signature), thawed(fragment.value)
    old = previous.parts if previous is not None and type(previous.parts) is type(fragment.parts) else None
    if isinstance(fragment.parts, dict):
        known = old or {}
        pairs = {key: detached(part, known.get(key), value[key]) for key, part in fragment.parts.items()}
        parts = {key: pair[0] for key, pair in pairs.items()}
        return Fragment({key: part.value for key, part in parts.items()}, parts), {
            key: pair[1] for key, pair in pairs.items()
        }
    known = old or ()
    pairs = [
        detached(part, known[index] if index < len(known) else None, item)
        for index, (part, item) in enumerate(zip(fragment.parts, value))
    ]
    parts = tuple(pair[0] for pair in pairs)
    return Fragment([part.value for part in parts], parts), [pair[1] for pair in pairs]
