"""The repository delivery.yaml lab block, the cutover skip list and the scheduler runtime config schema."""

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import jsonschema
import yaml

DELIVERY = "delivery.yaml"
WORKFLOWS = ".github/workflows"
WORKFLOW_SUFFIXES = (".yml", ".yaml")
LAB_OFF = "AGENTIHOOKS_LAB_OFF"
OUTPUT = "GITHUB_OUTPUT"
ALL_MUTANTS = "all"
PROG = "python -m scripts.ci_lab"
CLASSES = ("hotfix", "pull_request", "queue", "promotion", "backfill")
SHEDDABLE = ("backfill", "promotion", "queue")
FRACTION = {"type": "number", "minimum": 0, "maximum": 1}
LAB_SCHEMA = {
    "type": "object",
    "required": ["authoritative", "mutation", "check_prefix"],
    "additionalProperties": False,
    "properties": {
        "authoritative": {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True},
        "mutation": {
            "type": "object",
            "required": ["run_on", "mutants_per_changed_line", "arid"],
            "additionalProperties": False,
            "properties": {
                "run_on": {"enum": ["every_push", "ready"]},
                "mutants_per_changed_line": {"oneOf": [{"const": ALL_MUTANTS}, {"type": "integer", "minimum": 1}]},
                "arid": {"type": "array", "items": {"type": "string", "minLength": 1}},
            },
        },
        "check_prefix": {"type": "string", "minLength": 1},
    },
}
DELIVERY_SCHEMA = {"type": "object", "required": ["lab"], "properties": {"lab": LAB_SCHEMA}}
RUNTIME_SCHEMA = {
    "type": "object",
    "required": ["classes", "budget", "backfill"],
    "additionalProperties": False,
    "properties": {
        "classes": {"type": "array", "items": {"enum": list(CLASSES)}, "minItems": 1, "uniqueItems": True},
        "budget": {
            "type": "object",
            "required": ["coreMinutesPerDay", "shed"],
            "additionalProperties": False,
            "properties": {
                "coreMinutesPerDay": {"type": "number", "exclusiveMinimum": 0},
                "shed": {
                    "type": "object",
                    "propertyNames": {"enum": list(SHEDDABLE)},
                    "additionalProperties": FRACTION,
                },
            },
        },
        "backfill": {
            "type": "object",
            "required": ["auditShare", "moduleSweeps"],
            "additionalProperties": False,
            "properties": {"auditShare": FRACTION, "moduleSweeps": {"type": "boolean"}},
        },
    },
}


class DeliveryError(ValueError):
    """A delivery.yaml lab block or a runtime config that breaks its schema."""


def _validate(document: object, schema: Mapping[str, object], name: str) -> None:
    """Raise DeliveryError naming the failing path when the document breaks the schema."""
    try:
        jsonschema.validate(document, schema)
    except jsonschema.ValidationError as error:
        where = ".".join(str(part) for part in error.absolute_path)
        raise DeliveryError(f"{name} {where}: {error.message}") from error


def gates(root: Path) -> frozenset[str]:
    """Return the job ids of every workflow under the tree's workflow folder."""
    names: set[str] = set()
    for path in sorted((root / WORKFLOWS).iterdir()):
        if path.suffix in WORKFLOW_SUFFIXES:
            names.update(yaml.safe_load(path.read_text())["jobs"])
    return frozenset(names)


def lab(root: Path) -> Mapping[str, Any]:
    """Return the lab block of the tree's delivery.yaml, validated against the schema and the tree's own gates."""
    document = yaml.safe_load((root / DELIVERY).read_text())
    _validate(document, DELIVERY_SCHEMA, DELIVERY)
    block = document["lab"]
    unknown = sorted(set(block["authoritative"]) - gates(root))
    if unknown:
        raise DeliveryError(f"lab.authoritative names unknown gates: {', '.join(unknown)}")
    return block


def authoritative(grader_root: Path) -> tuple[str, ...]:
    """Return the gates the lab grades authoritatively, read only from the grader copy of delivery.yaml."""
    return tuple(lab(grader_root)["authoritative"])


def cutover(grader_root: Path, environ: Mapping[str, str]) -> list[str]:
    """Return the gates Actions skips: the authoritative gates, or none while the lab is switched off."""
    if environ.get(LAB_OFF) == "true":
        return []
    return list(authoritative(grader_root))


def runtime(document: Mapping[str, object]) -> Mapping[str, object]:
    """Validate the scheduler runtime config the chart mounts and return it."""
    _validate(document, RUNTIME_SCHEMA, "runtime")
    return document


def load_runtime(path: Path) -> Mapping[str, object]:
    """Read and validate the mounted scheduler runtime config file."""
    return runtime(yaml.safe_load(path.read_text()))


def main(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    """Run cutover, which appends skip=<json> to GITHUB_OUTPUT, or runtime, which validates a runtime config file.

    Either exits 1 when its input is invalid.
    """
    parser = argparse.ArgumentParser(prog=PROG)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("cutover").add_argument("--grader-root", type=Path, required=True)
    commands.add_parser("runtime").add_argument("path", type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "runtime":
            load_runtime(arguments.path)
            return 0
        skip = cutover(arguments.grader_root, environ)
    except DeliveryError as error:
        print(error, file=sys.stderr)
        return 1
    with Path(environ[OUTPUT]).open("a") as output:
        output.write(f"skip={json.dumps(skip)}\n")
    return 0
