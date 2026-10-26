import json
import random
import secrets
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

SCHEMAS = Path(__file__).parent
KINDS = ("case", "evidence", "verdict")


class SchemaError(ValueError):
    pass


def _validator(kind: str) -> Draft202012Validator:
    if kind not in KINDS:
        raise ValueError(f"unknown schema {kind}")
    return Draft202012Validator(json.loads((SCHEMAS / f"{kind}.schema.json").read_text()))


def validate(kind: str, doc: object) -> dict:
    error = best_match(_validator(kind).iter_errors(doc))
    if error is not None:
        where = "/".join(str(part) for part in error.absolute_path) or "document"
        raise SchemaError(f"{kind} {where}: {error.message}")
    if kind == "evidence":
        ids = [item["id"] for item in doc["items"]]
        duplicates = sorted({key for key in ids if ids.count(key) > 1})
        if duplicates:
            raise SchemaError(f"evidence items: duplicate id {', '.join(duplicates)}")
    return doc


def _load(kind: str, path: Path) -> dict:
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise SchemaError(f"{path.name} is not JSON: {error}") from None
    return validate(kind, doc)


def load_case(path: Path) -> dict:
    return _load("case", path)


def load_evidence(path: Path) -> dict:
    return _load("evidence", path)


def load_verdict(path: Path) -> dict:
    return _load("verdict", path)


def fresh_seed() -> int:
    return secrets.randbits(64)


def shuffled(items: list, seed: int | None = None) -> list:
    return random.Random(fresh_seed() if seed is None else seed).sample(items, len(items))
