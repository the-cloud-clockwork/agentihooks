"""Structural patches between two JSON values; static/js/patch.js applies the same format in the page.

A patch replaces a value ({"v": new}), deletes a key ({"d": 1}), patches an object by key ({"o": {...}}), or
patches a list: drop its first items and append new ones ({"drop", "add"}), upsert items by id ({"u"}), and reorder
by id ({"ids"}).
"""


def same(a, b):
    """JSON equality: unlike ==, true is not 1 and 1.0 is not 1."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same(value, b[key]) for key, value in a.items())
    if isinstance(a, list):
        return len(a) == len(b) and all(map(same, a, b))
    return a == b


def diff(old, new):
    if same(old, new):
        return None
    if isinstance(old, dict) and isinstance(new, dict):
        keys = {key: {"d": 1} for key in old if key not in new}
        keys.update({key: {"v": new[key]} for key in new if key not in old})
        keys.update({key: part for key in new if key in old and (part := diff(old[key], new[key])) is not None})
        return {"o": keys}
    if isinstance(old, list) and isinstance(new, list):
        return list_diff(old, new)
    return {"v": new}


def ids_of(items):
    ids = [item.get("id") if isinstance(item, dict) else None for item in items]
    if all(isinstance(i, str) for i in ids) and len(set(ids)) == len(ids):
        return ids
    return None


def overlap(old, new):
    """The fewest leading items of old to drop so the rest of old starts new; all of old at worst."""
    for drop in range(len(old)):
        kept = len(old) - drop
        if kept <= len(new) and same(old[drop], new[0]) and same(old[drop:], new[:kept]):
            return drop
    return len(old)


def list_diff(old, new):
    old_ids, new_ids = ids_of(old), ids_of(new)
    if old_ids is None or new_ids is None:
        drop = overlap(old, new)
        return {"drop": drop, "add": new[len(old) - drop :], "u": []}
    before = dict(zip(old_ids, old, strict=True))
    kept = len(old_ids) - overlap(old_ids, new_ids)
    if kept * 2 < len(new_ids):
        return {"ids": new_ids, "u": [item for item in new if not same(before.get(item["id"]), item)]}
    return {
        "drop": len(old_ids) - kept,
        "add": new[kept:],
        "u": [i for i in new[:kept] if not same(before[i["id"]], i)],
    }


def apply(value, patch):
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
