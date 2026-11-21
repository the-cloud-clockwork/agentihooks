import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from .rows import encode

if TYPE_CHECKING:
    from .fragments import Fragment

SCALARS = frozenset({str, int, float, bool, type(None)})
MISSING = object()
MAPPING: type[dict[str, Any]] = dict


class Owner:
    __slots__ = ("fragment",)

    def __init__(self, fragment: "Fragment | None") -> None:
        self.fragment = fragment


class TrackedDict(dict[str, Any]):
    __slots__ = ("owner", "root")
    owner: Owner
    root: bool


class TrackedList(list[Any]):
    __slots__ = ("owner", "root")
    owner: Owner
    root: bool


def changing(write: Callable[..., Any]) -> Callable[..., Any]:
    def changed(container: TrackedDict | TrackedList, *args: Any, **kwargs: Any) -> Any:
        container.owner.fragment = None
        return write(container, *args, **kwargs)

    return changed


def assign(container: TrackedDict, key: str, value: object) -> None:
    if container.get(key, MISSING) is not value:
        container.owner.fragment = None
    MAPPING.__setitem__(container, key, value)


def setdefault(container: TrackedDict, key: str, default: object = None) -> object:
    if key not in container:
        container.owner.fragment = None
    return MAPPING.setdefault(container, key, default)


def pop(container: TrackedDict, key: str, *default: object) -> object:
    if key in container:
        container.owner.fragment = None
    return MAPPING.pop(container, key, *default)


def plain(container: TrackedDict | TrackedList, memo: dict[int, object] | None = None) -> object:
    return json.loads(encode(container))


def shallow(container: TrackedDict | TrackedList) -> object:
    return dict(container) if isinstance(container, dict) else list(container)


DICT_WRITES = ("__delitem__", "__ior__", "clear", "popitem", "update")
LIST_WRITES = (
    *("__setitem__", "__delitem__", "__iadd__", "__imul__", "append", "extend"),
    *("insert", "pop", "remove", "clear", "sort", "reverse"),
)
for name in DICT_WRITES:
    setattr(TrackedDict, name, changing(getattr(dict, name)))
for name in LIST_WRITES:
    setattr(TrackedList, name, changing(getattr(list, name)))
for name, method in (("__setitem__", assign), ("setdefault", setdefault), ("pop", pop)):
    setattr(TrackedDict, name, method)
for kind in (TrackedDict, TrackedList):
    setattr(kind, "__deepcopy__", plain)
    setattr(kind, "__copy__", shallow)


def adopted(value: object, owner: Owner, root: bool = True) -> object:
    held: TrackedDict | TrackedList
    kind = type(value)
    if kind is dict and all(type(key) is str for key in cast(dict[object, object], value)):
        items = cast(dict[str, object], value).items()
        held = TrackedDict({key: adopted(item, owner, False) for key, item in items})
    elif kind is list:
        held = TrackedList([adopted(item, owner, False) for item in cast(list[object], value)])
    elif kind in SCALARS:
        return value
    else:
        return adopted(json.loads(encode(value)), owner, root)
    held.owner, held.root = owner, root
    return held


def clean(value: object) -> "Fragment | None":
    if (type(value) is TrackedDict or type(value) is TrackedList) and value.root:
        return value.owner.fragment
    return None


def children(fragment: "Fragment") -> "dict[str, Fragment] | tuple[Fragment, ...] | None":
    return cast("dict[str, Fragment] | tuple[Fragment, ...] | None", fragment.parts)


def branch(fragment: "Fragment") -> object:
    parts = children(fragment)
    if parts is None:
        return adopted(fragment.value, Owner(fragment))
    if isinstance(parts, tuple):
        return [branch(part) for part in parts]
    return {key: branch(part) for key, part in parts.items()}


def meta(fragment: "Fragment") -> object:
    parts = children(fragment)
    if not isinstance(parts, dict):
        return branch(fragment)
    return {
        key: dict(cast(dict[str, object], part.value))
        if key == "stamps" and isinstance(children(part), dict)
        else branch(part)
        for key, part in parts.items()
    }


def copied(document: "Fragment") -> dict[str, Any]:
    parts = cast("dict[str, Fragment]", document.parts)
    return {key: meta(part) if key == "_meta" else branch(part) for key, part in parts.items()}
