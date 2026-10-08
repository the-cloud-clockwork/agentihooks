from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

MODES = ("finders", "classifier", "both")
ACTIONS = ("send-back", "strip", "flag")
DEFAULT_QUESTION = "Does this finding go against the filter intent?"
_FINDER_KEYS = frozenset({"regex", "reason", "script"})


class FilterSchemaError(ValueError):
    pass


@dataclass(frozen=True)
class Finder:
    pattern: re.Pattern | None
    reason: str
    script: str = ""


@dataclass(frozen=True)
class FilterSpec:
    paths: tuple[str, ...] = ()
    finders: tuple[Finder, ...] = ()
    mode: str = "both"
    intent: str = ""
    intent_from: str = ""
    question: str = DEFAULT_QUESTION
    action: str = "send-back"
    max_rounds: int = 3


def _strings(key: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise FilterSchemaError(f"{key} must be a list of non-empty strings")
    return tuple(value)


def _text(key: str, value: object) -> str:
    if not isinstance(value, str):
        raise FilterSchemaError(f"{key} must be text")
    return value


def _choice(key: str, value: object, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise FilterSchemaError(f"{key} must be one of {', '.join(allowed)}, got {value!r}")
    return value


def _rounds(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise FilterSchemaError(f"max_rounds must be a whole number of at least 1, got {value!r}")
    return value


def _finder(index: int, raw: object) -> Finder:
    if not isinstance(raw, dict) or len({"regex", "script"} & raw.keys()) != 1:
        raise FilterSchemaError(f"finder {index} must be a mapping with exactly one regex or script")
    unknown = sorted(set(raw) - _FINDER_KEYS)
    if unknown:
        raise FilterSchemaError(f"finder {index} has unknown keys: {', '.join(map(str, unknown))}")
    reason = _text(f"finder {index} reason", raw.get("reason", "matched a finder"))
    if "script" in raw:
        name = _text(f"finder {index} script", raw["script"])
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
            raise FilterSchemaError(f"finder {index} script must be a finder name")
        return Finder(None, reason, name)
    try:
        pattern = re.compile(_text(f"finder {index} regex", raw["regex"]))
    except re.error as error:
        raise FilterSchemaError(f"finder {index} regex does not compile: {error}") from None
    return Finder(pattern, reason)


def _finders(value: object) -> tuple[Finder, ...]:
    if not isinstance(value, list):
        raise FilterSchemaError("finders must be a list")
    return tuple(_finder(i, raw) for i, raw in enumerate(value))


_FIELDS = {
    "paths": lambda v: _strings("paths", v),
    "finders": _finders,
    "mode": lambda v: _choice("mode", v, MODES),
    "intent": lambda v: _text("intent", v),
    "intent_from": lambda v: _text("intent_from", v),
    "question": lambda v: _text("question", v),
    "action": lambda v: _choice("action", v, ACTIONS),
    "max_rounds": _rounds,
}


def parse(raw: object) -> FilterSpec:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise FilterSchemaError("a filter must be a mapping of keys")
    unknown = sorted(str(key) for key in set(raw) - set(_FIELDS))
    if unknown:
        raise FilterSchemaError(f"unknown keys: {', '.join(unknown)}")
    return FilterSpec(**{key: _FIELDS[key](value) for key, value in raw.items()})


def load(path: str | Path) -> FilterSpec:
    try:
        raw = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as error:
        raise FilterSchemaError(f"cannot read the filter: {error}") from None
    return parse(raw)
