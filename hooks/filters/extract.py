from __future__ import annotations

from dataclasses import dataclass

_SINGLE_FIELD = {"Write": "content", "Edit": "new_string", "NotebookEdit": "new_source"}
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


def pieces(tool_name: str, tool_input: dict) -> list[Piece]:
    field = _SINGLE_FIELD.get(tool_name)
    if field:
        value = tool_input.get(field)
        return [Piece((field,), value)] if isinstance(value, str) else []
    if tool_name == "MultiEdit":
        return _items(tool_input, "edits", "new_string")
    if isinstance(tool_input.get("text"), str):
        return [Piece(("text",), tool_input["text"])]
    return _items(tool_input, "evidence")


def _set(patch: dict, where: tuple, value: str) -> None:
    if len(where) == 1:
        patch[where[0]] = value
        return
    key, index = where[0], where[1]
    if len(where) == 3:
        patch[key][index] = {**patch[key][index], where[2]: value}
    else:
        patch[key][index] = value


def rewrite(tool_input: dict, replaced: dict[tuple, str]) -> dict:
    patch = {}
    for where in replaced:
        if len(where) > 1 and where[0] not in patch:
            patch[where[0]] = list(tool_input[where[0]])
    for where, value in replaced.items():
        _set(patch, where, value)
    return patch
