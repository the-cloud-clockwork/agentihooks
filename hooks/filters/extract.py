from __future__ import annotations

import copy
from dataclasses import dataclass

_SINGLE_FIELD = {"Write": "content", "Edit": "new_string", "NotebookEdit": "new_source"}
_EVIDENCE_FIELDS = ("claim", "excerpt")
_PATH_FIELDS = ("file_path", "notebook_path", "path")


@dataclass(frozen=True)
class Piece:
    where: tuple
    text: str


def target_path(tool_input: dict) -> str:
    return next((str(tool_input[key]) for key in _PATH_FIELDS if isinstance(tool_input.get(key), str)), "")


def _items(tool_input: dict, key: str, field: str | None = None) -> list[Piece]:
    items = tool_input.get(key)
    if not isinstance(items, list):
        return []
    pieces = []
    for i, item in enumerate(items):
        value = (item.get(field) if isinstance(item, dict) else None) if field else item
        if isinstance(value, str):
            pieces.append(Piece((key, i, field) if field else (key, i), value))
    return pieces


def _case(tool_input: dict) -> list[Piece]:
    case = tool_input.get("case")
    if not isinstance(case, dict):
        return []
    found = [Piece(("case", "intent"), case["intent"])] if isinstance(case.get("intent"), str) else []
    points = case.get("points")
    if isinstance(points, list):
        found += [Piece(("case", "points", i), point) for i, point in enumerate(points) if isinstance(point, str)]
    return found


def _evidence(tool_input: dict) -> list[Piece]:
    evidence = tool_input.get("evidence")
    items = evidence.get("items") if isinstance(evidence, dict) else None
    if not isinstance(items, list):
        return []
    return [
        Piece(("evidence", "items", i, field), item[field])
        for i, item in enumerate(items)
        if isinstance(item, dict)
        for field in _EVIDENCE_FIELDS
        if isinstance(item.get(field), str)
    ]


def units(tool_name: str | None, found: list[Piece]) -> list[Piece]:
    if tool_name != "judge":
        return found
    grouped: dict[tuple, list[str]] = {}
    for piece in found:
        key = piece.where[:3] if piece.where[0] == "evidence" else piece.where[:1]
        grouped.setdefault(key, []).append(piece.text)
    return [Piece(key, "\n".join(texts)) for key, texts in grouped.items()]


def pieces(tool_name: str | None, tool_input: dict) -> list[Piece]:
    field = _SINGLE_FIELD.get(tool_name)
    if field:
        value = tool_input.get(field)
        return [Piece((field,), value)] if isinstance(value, str) else []
    if tool_name == "MultiEdit":
        return _items(tool_input, "edits", "new_string")
    text = tool_input.get("text")
    found = [Piece(("text",), text)] if isinstance(text, str) else []
    return found + _items(tool_input, "evidence") + _case(tool_input) + _evidence(tool_input)


def _node(patch: dict, tool_input: dict, where: tuple):
    node = patch.setdefault(where[0], copy.deepcopy(tool_input[where[0]]))
    for key in where[1:-1]:
        node = node[key]
    return node


def _set(patch: dict, tool_input: dict, where: tuple, value: str) -> None:
    if len(where) == 1:
        patch[where[0]] = value
        return
    _node(patch, tool_input, where)[where[-1]] = value


def rewrite(tool_input: dict, replaced: dict[tuple, str], dropped: list | tuple = ()) -> dict:
    patch = {}
    for where, value in replaced.items():
        _set(patch, tool_input, where, value)
    for where in sorted((w for w in dropped if len(w) > 1 and isinstance(w[-1], int)), reverse=True):
        del _node(patch, tool_input, where)[where[-1]]
    return patch
