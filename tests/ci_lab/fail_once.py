"""A test helper that fails a test's first run and passes its later runs, so the lab retry marks it flaky once."""

from pathlib import Path


def fail_once(node: str, fallback: Path) -> None:
    """Fail the first run of the test `node` on this head and pass every later run."""
