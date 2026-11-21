import copy
import json
from collections.abc import Callable
from functools import partial
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


class Writing:
    __slots__ = ("method",)

    def __init__(self, method: Callable[..., Any]) -> None:
        self.method = method

    def __get__(self, instance: Tracked, owner: object) -> Callable[..., Any]:
        return partial(self._write, instance)

    def _write(self, instance: Tracked, *args: Any, **kwargs: Any) -> Any:
        instance.mark.fragment = None
        return self.method(instance, *args, **kwargs)


def setdefault(self: TrackedDict, key: str, default: Any = None) -> Any:
    if key not in self:
        self.mark.fragment = None
    return super(TrackedDict, self).setdefault(key, default)


def pop(self: TrackedDict, key: str, *default: Any) -> Any:
    if key in self:
        self.mark.fragment = None
    return super(TrackedDict, self).pop(key, *default)


def shallow(self: Tracked) -> object:
    return dict(self) if isinstance(self, dict) else list(self)


def deep(self: Tracked, memo: dict[int, Any]) -> object:
    if isinstance(self, dict):
        return {key: copy.deepcopy(item, memo) for key, item in self.items()}
    return [copy.deepcopy(item, memo) for item in self]


for _name in ("__init__", "__setitem__", "__delitem__", "__ior__", "clear", "popitem", "update"):
    setattr(TrackedDict, _name, Writing(getattr(dict, _name)))
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
    setattr(TrackedList, _name, Writing(getattr(list, _name)))
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
