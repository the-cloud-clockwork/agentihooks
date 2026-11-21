import copy
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from .rows import encode

if TYPE_CHECKING:
    from .fragments import Fragment

SCALARS = frozenset({str, int, float, bool, type(None)})


class Mark:
    __slots__ = ("fragment",)

    def __init__(self, fragment: "Fragment | None") -> None:
        self.fragment = fragment


class TrackedDict(dict[str, Any]):
    __slots__ = ("mark", "root")
    mark: Mark
    root: bool


class TrackedList(list[Any]):
    __slots__ = ("mark", "root")
    mark: Mark
    root: bool


Tracked = TrackedDict | TrackedList
LISTS = frozenset({list, TrackedList})
DICTS = frozenset({dict, TrackedDict})
TRACKED = frozenset({TrackedDict, TrackedList})


def marking(method: Callable[..., Any]) -> Callable[..., Any]:
    def written(self: Tracked, *args: Any, **kwargs: Any) -> Any:
        self.mark.fragment = None
        return method(self, *args, **kwargs)

    return written


def setdefault(self: TrackedDict, key: str, default: Any = None) -> Any:
    if key not in self:
        self.mark.fragment = None
    return cast(Any, dict).setdefault(self, key, default)


def pop(self: TrackedDict, key: str, *default: Any) -> Any:
    if key in self:
        self.mark.fragment = None
    return cast(Any, dict).pop(self, key, *default)


def shallow(self: Tracked) -> object:
    return dict(self) if isinstance(self, dict) else list(self)


def deep(self: Tracked, memo: dict[int, Any]) -> object:
    if isinstance(self, dict):
        mapping: dict[str, Any] = {}
        memo[id(self)] = mapping
        mapping.update((copy.deepcopy(key, memo), copy.deepcopy(item, memo)) for key, item in self.items())
        return mapping
    items: list[Any] = []
    memo[id(self)] = items
    items.extend(copy.deepcopy(item, memo) for item in self)
    return items


for _name in ("__init__", "__setitem__", "__delitem__", "__ior__", "clear", "popitem", "update"):
    setattr(TrackedDict, _name, marking(getattr(dict, _name)))
for _name in (
    "__init__",
    "__setitem__",
    "__delitem__",
    "__iadd__",
    "__imul__",
    "append",
    "extend",
    "insert",
    "pop",
    "remove",
    "clear",
    "sort",
    "reverse",
):
    setattr(TrackedList, _name, marking(getattr(list, _name)))
for _kind in (TrackedDict, TrackedList):
    setattr(_kind, "__copy__", shallow)
    setattr(_kind, "__deepcopy__", deep)
setattr(TrackedDict, "setdefault", setdefault)
setattr(TrackedDict, "pop", pop)


def held(value: object) -> "Fragment | None":
    if type(value) in TRACKED and cast(Tracked, value).root:
        return cast(Tracked, value).mark.fragment
    return None


def made(value: object, mark: Mark, root: bool = False) -> object:
    kind = type(value)
    if kind in LISTS:
        items = TrackedList.__new__(TrackedList)
        super(TrackedList, items).extend(made(item, mark) for item in cast(list[object], value))
        container: Tracked = items
    elif kind in DICTS and all(type(key) is str for key in cast(dict[object, object], value)):
        mapping = TrackedDict.__new__(TrackedDict)
        super(TrackedDict, mapping).update(
            (key, made(item, mark)) for key, item in cast(dict[str, object], value).items()
        )
        container = mapping
    elif kind in SCALARS:
        return value
    else:
        return made(json.loads(encode(value)), mark, root)
    container.mark, container.root = mark, root
    return container


def bound(fragment: "Fragment") -> object:
    return made(fragment.value, Mark(fragment), True)


def copied(fragment: "Fragment") -> object:
    parts = cast("dict[str, Fragment] | tuple[Fragment, ...] | None", fragment.parts)
    if parts is None:
        return bound(fragment)
    if isinstance(parts, tuple):
        return [copied(part) for part in parts]
    return {key: copied(part) for key, part in parts.items()}
