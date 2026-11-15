"""Test units from the dependency manifest: a content fingerprint per test and file sized test batches."""

from collections.abc import Mapping, Sequence
from typing import Any

PYTEST_INVOCATION = (
    "pytest",
    "--dist",
    "loadgroup",
    "--timeout=60",
    "-p",
    "tests.pass_record",
    "-p",
    "tests.flake_ledger",
)


def broad(record: Mapping[str, Any]) -> bool:
    """Return whether a manifest record's dependencies are unknown, so it is keyed on the measured tree."""
    return False


def fingerprints(
    manifest: Mapping[str, Any], tree: Mapping[str, bytes], invocation: Sequence[str] = PYTEST_INVOCATION
) -> dict[str, str]:
    """Return a content key per manifest test over the repository tree it runs against."""
    return {}


def batches(manifest: Mapping[str, Any], durations: Mapping[str, float], target: float) -> list[tuple[str, ...]]:
    """Return the manifest tests cut into batches of one test file each, split to fit the target seconds."""
    return []
