import json
import re
from collections import Counter
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from referencing import Registry, Resource

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas" / "swarm_v2"
FAILURES: Counter = Counter()


def load(schemas: Path = SCHEMAS) -> dict:
    docs = [json.loads(path.read_text()) for path in sorted(schemas.glob("*.json"))]
    registry = Registry().with_resources((doc["$id"], Resource.from_contents(doc)) for doc in docs if "$id" in doc)
    compat = json.loads((schemas / "compatibility.json").read_text())
    common = registry.contents("urn:swarm-v2:common")
    validators = {
        name: Draft202012Validator(json.loads((schemas / spec["schema"]).read_text()), registry=registry)
        for name, spec in compat["contracts"].items()
    }
    return {"compat": compat, "validators": validators, "identifier": common["$defs"]["identifier"]["pattern"]}


def write_version(contracts: dict, name: str) -> str:
    spec = contracts["compat"]["contracts"][name]
    return f"{spec['major']}.{spec['write_minor']}"


def failures() -> dict[str, int]:
    return {f"{contract}/{reason}": count for (contract, reason), count in FAILURES.items()}


def _refusal(contracts: dict, doc, error: str, reason: str, detail: str, retry: str = "new_request") -> dict:
    op = doc.get("operation_id") if isinstance(doc, dict) else None
    known = isinstance(op, str) and re.fullmatch(contracts["identifier"], op)
    return {
        "schema_version": write_version(contracts, "error"),
        "operation_id": op if known else "unknown",
        "error": error,
        "reason": reason,
        "retry": retry,
        "detail": detail,
    }


def _describe(error) -> str:
    path = "/".join(str(part) for part in error.absolute_path) or "<root>"
    if error.validator == "required":
        return f"{path}: missing " + ", ".join(k for k in error.validator_value if k not in error.instance)
    return f"{path}: fails {error.validator}"


def _problem(contracts: dict, name: str, doc) -> tuple[str, str] | None:
    if not isinstance(doc, dict):
        return "not_an_object", "a record must be a JSON object"
    spec = contracts["compat"]["contracts"][name]
    version = doc.get("schema_version")
    match = re.fullmatch(r"(\d+)\.(\d+)", version) if isinstance(version, str) else None
    if not match:
        return "missing_version", "schema_version must be MAJOR.MINOR"
    major, minor = int(match[1]), int(match[2])
    if major != spec["major"]:
        return "unsupported_major", f"schema major {major} is not supported; this reader accepts major {spec['major']}"
    if minor < spec["oldest_minor"]:
        return (
            "unsupported_minor",
            f"schema minor {minor} is older than the oldest supported minor {spec['oldest_minor']}",
        )
    authority = doc.get("authority")
    missing = [k for k in spec["authority"] if not isinstance(authority, dict) or k not in authority]
    if missing:
        return "missing_authority", "authority lacks " + ", ".join(missing)
    error = best_match(contracts["validators"][name].iter_errors(doc))
    return ("schema_invalid", _describe(error)) if error else None


def check(contracts: dict, name: str, doc) -> dict | None:
    problem = _problem(contracts, name, doc)
    if problem is None:
        return None
    FAILURES[(name, problem[0])] += 1
    return _refusal(contracts, doc, "invalid_request", *problem)


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def admit(contracts: dict, store: Path, name: str, doc) -> dict:
    refusal = check(contracts, name, doc)
    if refusal:
        return refusal
    folder = store / name
    record = folder / f"{doc['operation_id']}.json"
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    if record.exists():
        if record.read_text() == text:
            return {"state": "replayed", "operation_id": doc["operation_id"]}
        detail = "operation id already holds a different record"
        return _refusal(contracts, doc, "revision_conflict", "operation_reused", detail)
    authority = doc["authority"]
    index = folder / "generations.json"
    generations = json.loads(index.read_text()) if index.exists() else {}
    if authority["task_generation"] < generations.get(authority["task_id"], 0):
        detail = "task generation is older than the accepted generation"
        return _refusal(contracts, doc, "stale_generation", "older_generation", detail, retry="never")
    folder.mkdir(parents=True, exist_ok=True)
    generations[authority["task_id"]] = authority["task_generation"]
    _write(index, json.dumps(generations, indent=2, sort_keys=True) + "\n")
    _write(record, text)
    return {"state": "accepted", "operation_id": doc["operation_id"]}
