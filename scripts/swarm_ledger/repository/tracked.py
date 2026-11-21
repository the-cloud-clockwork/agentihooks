import json
from typing import TYPE_CHECKING

from .rows import encode

if TYPE_CHECKING:
    from .fragments import Fragment


def held(value: object) -> "Fragment | None":
    return None


def bound(fragment: "Fragment") -> object:
    return json.loads(encode(fragment.value))


def copied(fragment: "Fragment") -> object:
    return json.loads(encode(fragment.value))
