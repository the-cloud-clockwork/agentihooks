"""Structural patches between two JSON values; static/js/patch.js applies the same format in the page.

A patch replaces a value ({"v": new}), deletes a key ({"d": 1}), patches an object by key ({"o": {...}}), or
patches a list: drop its first items and append new ones ({"drop", "add"}), upsert items by id ({"u"}), and reorder
by id ({"ids"}).
"""

from typing import Any, cast


def same(a: Any, b: Any, *, shared: bool = False) -> bool:
    """JSON equality: unlike ==, true is not 1 and 1.0 is not 1."""
    if shared and a is b:
        return True
    if type(a) is not type(b) or a != b:
        return False
    if isinstance(a, dict):
        return cast(dict[Any, Any], a).keys() == cast(dict[Any, Any], b).keys() and all(
            type(value) is type(cast(dict[Any, Any], b)[key])
            and (
                same(value, cast(dict[Any, Any], b)[key], shared=shared)
                if isinstance(value, (dict, list))
                else value == cast(dict[Any, Any], b)[key]
            )
            for key, value in cast(dict[Any, Any], a).items()
        )
    if isinstance(a, list):
        return len(cast(list[Any], a)) == len(cast(list[Any], b)) and all(
            same(left, right, shared=shared) for left, right in zip(cast(list[Any], a), cast(list[Any], b))
        )
    return a == b


def diff(old: Any, new: Any, *, shared: bool = False) -> dict[str, Any] | None:
    if same(old, new, shared=shared):
        return None
    if isinstance(old, dict) and isinstance(new, dict):
        keys = {key: {"d": 1} for key in cast(dict[Any, Any], old) if key not in cast(dict[Any, Any], new)}
        keys.update({key: {"v": cast(dict[Any, Any], new)[key]} for key in cast(dict[Any, Any], new) if key not in old})
        keys.update(
            {
                key: part
                for key in cast(dict[Any, Any], new)
                if key in old
                and (part := diff(cast(dict[Any, Any], old)[key], cast(dict[Any, Any], new)[key], shared=shared))
                is not None
            }
        )
        return {"o": keys}
    if isinstance(old, list) and isinstance(new, list):
        return list_diff(cast(list[Any], old), cast(list[Any], new), shared=shared)
    return {"v": new}


def ids_of(items: list[Any]) -> list[str] | None:
    ids = [cast(dict[Any, Any], item).get("id") if isinstance(item, dict) else None for item in items]
    if all(isinstance(i, str) for i in ids) and len(set(ids)) == len(ids):
        return cast(list[str], ids)
    return None


def overlap(old: list[Any], new: list[Any], *, shared: bool = False) -> int:
    """The fewest leading items of old to drop so the rest of old starts new; all of old at worst."""
    for drop in range(len(old)):
        kept = len(old) - drop
        if kept <= len(new) and same(old[drop], new[0], shared=shared) and same(old[drop:], new[:kept], shared=shared):
            return drop
    return len(old)


def list_diff(old: list[Any], new: list[Any], *, shared: bool = False) -> dict[str, Any]:
    old_ids, new_ids = ids_of(old), ids_of(new)
    if old_ids is None or new_ids is None:
        drop = overlap(old, new, shared=shared)
        return {"drop": drop, "add": new[len(old) - drop :], "u": []}
    before = {item["id"]: item for item in old}
    kept = len(old_ids) - overlap(old_ids, new_ids, shared=shared)
    if kept * 2 < len(new_ids):
        return {"ids": new_ids, "u": [item for item in new if not same(before.get(item["id"]), item, shared=shared)]}
    return {
        "drop": len(old_ids) - kept,
        "add": new[kept:],
        "u": [i for i in new[:kept] if not same(before[i["id"]], i, shared=shared)],
    }


def apply(value: Any, patch: dict[str, Any]) -> Any:
    if "v" in patch:
        return patch["v"]
    if "o" in patch:
        out = dict(value)
        for key, part in patch["o"].items():
            if "d" in part:
                out.pop(key, None)
            else:
                out[key] = apply(out.get(key), part)
        return out
    changed = {item["id"]: item for item in patch["u"]}
    if "ids" in patch:
        current = {item["id"]: item for item in value}
        return [changed.get(i, current.get(i)) for i in patch["ids"]]
    kept = [changed.get(item["id"], item) if changed else item for item in value[patch["drop"] :]]
    return kept + patch["add"]
