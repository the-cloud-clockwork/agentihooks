"""Canonical digests and the per kind unit keys of the CI lab.

A key names every input that can change a verdict and starts with its kind and schema version,
so a schema bump is a cold cache. The day, the run id, the branch and the author never enter a key.
docs/reference/ci-lab.md holds the digest specification.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from scripts.ci_mutation import stats_cache
from scripts.profiles.sources import blob as blob_id

__all__ = [
    "LOCKFILE",
    "SCHEMAS",
    "GradeInputs",
    "MutantInputs",
    "StaticInputs",
    "StatsInputs",
    "TestInputs",
    "Toolchain",
    "blob_id",
    "digest",
    "grade_key",
    "mutant_key",
    "static_key",
    "stats_key",
    "test_key",
]

LOCKFILE = ".github/test-requirements.lock"
SCHEMAS: dict[str, int] = {"static": 1, "test": 1, "stats": stats_cache.SCHEMA, "mutant": 1, "grade": 1}


@dataclass(frozen=True)
class Toolchain:
    """What a unit runs on: interpreter, tool versions, lockfile, pyproject, worker image and environment key."""

    interpreter: str
    tools: Mapping[str, str]
    lockfile: str
    pyproject: str
    image: str
    environment: Mapping[str, str]


@dataclass(frozen=True)
class StaticInputs:
    """The inputs of one static gate: the files it grades at head and base, the grader tree, and the event names it
    reads with their values."""

    gate: str
    head: Mapping[str, str]
    base: Mapping[str, str]
    grader: str
    toolchain: Toolchain
    event: Mapping[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True)
class TestInputs:
    """The inputs of one test batch: per test fingerprints, files opened outside the manifest, the invocation."""

    tests: Mapping[str, str]
    opened: Mapping[str, str]
    invocation: tuple[str, ...]
    toolchain: Toolchain


@dataclass(frozen=True)
class StatsInputs:
    """The inputs of one test file's mutation stats, field for field the stats cache identity."""

    path: str
    inputs: Mapping[str, str]
    scope: Mapping[str, tuple[int, ...]]
    measured: bool
    discovery: str
    sources: Mapping[str, str]
    options: tuple[str, ...]
    interpreter: str
    platform: str
    versions: Mapping[str, str]


@dataclass(frozen=True)
class MutantInputs:
    """The inputs of the mutants of one changed function."""

    module: str
    function: str
    definition: str
    module_code: str
    names: frozenset[str]
    mutmut: str
    runner: str
    covering: Mapping[str, str]
    toolchain: Toolchain


@dataclass(frozen=True)
class GradeInputs:
    """The identity of one grade."""

    repository: str
    head: str
    base: str
    tier: str
    grader: str


def digest(record: dict[str, object]) -> str:
    """Return the sha256 hex digest of a record's canonical JSON: sorted keys, compact separators."""
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _key(kind: str, inputs: dict[str, object]) -> str:
    schema = SCHEMAS[kind]
    return f"{kind}-v{schema}-{digest({'schema': schema, 'kind': kind, 'inputs': inputs})}"


def _toolchain(toolchain: Toolchain) -> dict[str, object]:
    return {
        "interpreter": toolchain.interpreter,
        "tools": dict(toolchain.tools),
        "lockfile": toolchain.lockfile,
        "pyproject": toolchain.pyproject,
        "image": toolchain.image,
        "environment": dict(toolchain.environment),
    }


def static_key(inputs: StaticInputs) -> str:
    """Return the key of one static gate's verdict."""
    return _key(
        "static",
        {
            "gate": inputs.gate,
            "head": dict(inputs.head),
            "base": dict(inputs.base),
            "grader": inputs.grader,
            "toolchain": _toolchain(inputs.toolchain),
        },
    )


def test_key(inputs: TestInputs) -> str:
    """Return the key of one test batch's verdict."""
    return _key(
        "test",
        {
            "tests": dict(inputs.tests),
            "opened": dict(inputs.opened),
            "invocation": list(inputs.invocation),
            "toolchain": _toolchain(inputs.toolchain),
        },
    )


def stats_key(inputs: StatsInputs) -> str:
    """Return the key of one test file's mutation stats.

    The record is hashed in the stats cache's own JSON form, default separators, so the digest
    equals the stats cache identity and the lab reuses the stats the cache already holds.
    """
    schema = SCHEMAS["stats"]
    context = {
        "schema": schema,
        "sources": dict(inputs.sources),
        "options": list(inputs.options),
        "runtime": [inputs.interpreter, inputs.platform, dict(inputs.versions)],
    }
    identity = {
        "context": context,
        "path": inputs.path,
        "inputs": dict(inputs.inputs),
        "scope": {source: list(lines) for source, lines in inputs.scope.items()},
        "measured": inputs.measured,
        "discovery": inputs.discovery,
    }
    return f"stats-v{schema}-{hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()}"


def mutant_key(inputs: MutantInputs) -> str:
    """Return the key of the verdicts of one changed function's mutants."""
    return _key(
        "mutant",
        {
            "module": inputs.module,
            "function": inputs.function,
            "definition": inputs.definition,
            "module_code": inputs.module_code,
            "names": sorted(inputs.names),
            "mutmut": inputs.mutmut,
            "runner": inputs.runner,
            "covering": dict(inputs.covering),
            "toolchain": _toolchain(inputs.toolchain),
        },
    )


def grade_key(inputs: GradeInputs) -> str:
    """Return the key of one grade."""
    return _key(
        "grade",
        {
            "repository": inputs.repository,
            "head": inputs.head,
            "base": inputs.base,
            "tier": inputs.tier,
            "grader": inputs.grader,
        },
    )
