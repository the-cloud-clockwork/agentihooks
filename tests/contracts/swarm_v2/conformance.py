import copy
import hashlib
import importlib
import json
import sys
from collections.abc import Callable
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SCOPE_KEYS = ("project_id", "brain_id", "swarm_id")
FOREIGN_SCOPE = {
    "project_id": "github.com/synthetic-owner/another-project",
    "brain_id": "brain-outside-grant",
    "swarm_id": "swarm-outside-grant",
}
CANARY = "conformance-bearer-canary-5c1e"
ACCEPTED = {"state": "accepted"}
REPLAYED = {"state": "replayed"}
INVALID = {"error": "invalid_request", "retry": "new_request"}
STALE = {"error": "stale_generation", "retry": "never"}
CONFLICT = {"error": "revision_conflict", "retry": "new_request"}
FORBIDDEN = {"error": "forbidden_scope", "retry": "new_request"}
ADMITTED_FAMILIES = {
    "current": ACCEPTED,
    "previous-minor": ACCEPTED,
    "newer-minor": ACCEPTED,
    "future-major": INVALID,
    "missing-authority": INVALID,
    "previous-minor-missing-authority": INVALID,
}


def registry(schemas: Path) -> Registry:
    docs = [json.loads(path.read_text()) for path in sorted(schemas.glob("*.json"))]
    return Registry().with_resources((doc["$id"], Resource.from_contents(doc)) for doc in docs if "$id" in doc)


def _validator(schemas: Path, reg: Registry, compat: dict, contract: str) -> Draft202012Validator:
    schema = json.loads((schemas / compat["contracts"][contract]["schema"]).read_text())
    return Draft202012Validator(schema, registry=reg)


def _step(doc: dict, expect: dict) -> dict:
    return {"doc": doc, "expect": expect}


def _renamed(doc: dict, suffix: str) -> dict:
    out = copy.deepcopy(doc)
    out["operation_id"] = f"{doc['operation_id']}-{suffix}"
    return out


def _extension(current: dict) -> dict:
    doc = _renamed(current, "extension")
    doc["relay_provenance"] = {"source": "synthetic-newer-producer", "sha256": "0" * 64}
    doc["extensions"] = {"x-conformance": {"observed_by": "synthetic-relay", "durable": True}}
    return doc


def _invalid(current: dict) -> dict:
    doc = _renamed(current, "invalid")
    doc["trace_id"] = "not a hexadecimal trace"
    return doc


def _labelled(current: dict) -> dict:
    doc = _renamed(current, "labelled")
    doc["authority"]["display_label"] = "operator"
    return doc


def _generation(current: dict, delta: int, suffix: str) -> dict:
    doc = _renamed(current, suffix)
    doc["authority"]["task_generation"] += delta
    return doc


def _other_execution(current: dict) -> dict:
    doc = _renamed(current, "other-execution")
    doc["authority"]["execution_id"] += "-other"
    return doc


def _grant(authority: dict) -> dict:
    return {**{key: authority[key] for key in SCOPE_KEYS if key in authority}, "bearer": CANARY}


def _foreign(grant: dict) -> dict:
    key = next(key for key in SCOPE_KEYS if key in grant)
    return {**grant, key: FOREIGN_SCOPE[key]}


def _family_cases(contract: str, docs: dict[str, dict], grant: dict) -> list[dict]:
    cases = [
        {"family": family, "grant": grant, "steps": [_step(docs[family], expect)]}
        for family, expect in ADMITTED_FAMILIES.items()
        if family in docs
    ]
    current = docs["current"]
    cases += [
        {"family": "extension", "grant": grant, "steps": [_step(_extension(current), ACCEPTED)]},
        {"family": "invalid", "grant": grant, "steps": [_step(_invalid(current), INVALID)]},
        {"family": "unknown-authority", "grant": grant, "steps": [_step(_labelled(current), INVALID)]},
    ]
    return cases


def _sequence_cases(current: dict, grant: dict) -> list[dict]:
    reused = copy.deepcopy(current)
    reused["trace_id"] = "f" * 32
    cases = [
        {"family": "replay", "grant": grant, "steps": [_step(current, ACCEPTED), _step(current, REPLAYED)]},
        {"family": "reused-operation", "grant": grant, "steps": [_step(current, ACCEPTED), _step(reused, CONFLICT)]},
        {
            "family": "older-generation",
            "grant": grant,
            "steps": [_step(_generation(current, 1, "newer-generation"), ACCEPTED), _step(current, STALE)],
        },
        {
            "family": "held-generation",
            "grant": grant,
            "steps": [_step(current, ACCEPTED), _step(_other_execution(current), STALE)],
        },
        {
            "family": "newer-generation",
            "grant": grant,
            "steps": [_step(current, ACCEPTED), _step(_generation(current, 1, "newer-generation"), ACCEPTED)],
        },
    ]
    if any(key in grant for key in SCOPE_KEYS):
        cases.append({"family": "outside-grant", "grant": _foreign(grant), "steps": [_step(current, FORBIDDEN)]})
    return cases


def _validated_cases(docs: dict[str, dict]) -> list[dict]:
    current = docs["current"]
    validated = {**docs, "extension": _extension(current), "invalid": _invalid(current)}
    expect = {"future-major": "invalid", "invalid": "invalid"}
    return [
        {"family": family, "validate": doc, "expect": expect.get(family, "valid")} for family, doc in validated.items()
    ]


def build_matrix(fixtures: Path, compat: dict) -> dict:
    index = json.loads((fixtures / "index.json").read_text())["fixtures"]
    cases = []
    for contract, spec in compat["contracts"].items():
        docs = {e["family"]: json.loads((fixtures / e["file"]).read_text()) for e in index if e["contract"] == contract}
        if not spec["authority"]:
            found = _validated_cases(docs)
        else:
            grant = _grant(docs["current"]["authority"])
            found = _family_cases(contract, docs, grant) + _sequence_cases(docs["current"], grant)
        cases += [{"case": f"{contract}/{case['family']}", "contract": contract, **case} for case in found]
    return {"schema": "swarm-v2-conformance-matrix/1", "package": "SV2-VAL-01", "cases": cases}


def _judge_response(validator: Draft202012Validator, expect: dict, status: object, body: object) -> list[str]:
    text = json.dumps(body, sort_keys=True, default=str)
    failures = [] if CANARY not in text else ["the response discloses the caller bearer"]
    if not isinstance(body, dict):
        return [*failures, "the response body is not an object"]
    if "state" in expect:
        if not (isinstance(status, int) and 200 <= status < 300):
            failures.append(f"status {status} for an accepted record")
        if body.get("state") != expect["state"]:
            failures.append(f"state {body.get('state', body.get('error'))!r}, expected {expect['state']!r}")
        return failures
    if not (isinstance(status, int) and 400 <= status < 500):
        failures.append(f"status {status} for a refused record")
    if not validator.is_valid(body):
        failures.append("the refusal is not a valid error envelope")
    for key in ("error", "retry"):
        if body.get(key) != expect[key]:
            failures.append(f"{key} {body.get(key)!r}, expected {expect[key]!r}")
    return failures


def _judge_state(target, contract: str, step: dict, before: dict, after: dict) -> list[str]:
    doc = step["doc"]
    if "error" in step["expect"]:
        return [] if before == after else ["a refused record changed stored state"]
    if target.stored(contract, doc["operation_id"]) != doc:
        return ["the stored record differs from the accepted record"]
    if step["expect"] == REPLAYED and before != after:
        return ["a replay changed stored state"]
    return []


def _admission(case: dict, target, error_validator: Draft202012Validator) -> list[str]:
    failures = []
    for number, step in enumerate(case["steps"], 1):
        before = target.snapshot()
        status, body = target.submit(case["contract"], copy.deepcopy(step["doc"]), copy.deepcopy(case["grant"]))
        found = _judge_response(error_validator, step["expect"], status, body)
        found += _judge_state(target, case["contract"], step, before, target.snapshot())
        failures += [f"step {number}: {failure}" for failure in found]
    return failures


def run(matrix: dict, schemas: Path, factory: Callable[[Path], object], workdir: Path) -> dict:
    reg = registry(schemas)
    compat = json.loads((schemas / "compatibility.json").read_text())
    error_validator = _validator(schemas, reg, compat, "error")
    results = []
    for number, case in enumerate(matrix["cases"]):
        if "validate" in case:
            got = (
                "valid" if _validator(schemas, reg, compat, case["contract"]).is_valid(case["validate"]) else "invalid"
            )
            failures = [] if got == case["expect"] else [f"validated {got}, expected {case['expect']}"]
        else:
            failures = _admission(case, factory(workdir / f"case-{number:03d}"), error_validator)
        results.append({"case": case["case"], "passed": not failures, "failures": failures})
    passed = sum(result["passed"] for result in results)
    return {"contract_cases_passed": passed, "contract_cases_failed": len(results) - passed, "results": results}


def digest(path: Path) -> str:
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    sha = hashlib.sha256()
    for file in files:
        sha.update(file.name.encode() + b"\0" + file.read_bytes())
    return sha.hexdigest()


def publish(report: dict, repository: str, commit: str, schemas: Path, matrix: Path) -> dict:
    return {
        "schema": "swarm-v2-conformance-results/1",
        "package": "SV2-VAL-01",
        "repository": repository,
        "commit": commit,
        "schemas_sha256": digest(schemas),
        "matrix_sha256": digest(matrix),
        **report,
    }


def main(argv: list[str]) -> int:
    schemas, matrix_path, target, repository, commit, workdir = argv[:6]
    module, _, attr = target.partition(":")
    factory = getattr(importlib.import_module(module), attr)
    report = run(json.loads(Path(matrix_path).read_text()), Path(schemas), factory, Path(workdir))
    print(json.dumps(publish(report, repository, commit, Path(schemas), Path(matrix_path)), indent=2))
    return 0 if report["contract_cases_failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
