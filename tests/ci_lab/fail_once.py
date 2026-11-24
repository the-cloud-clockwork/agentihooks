"""A test helper that fails a test's first run and passes its later runs, so the lab retry marks it flaky once."""

import hashlib
import os
from pathlib import Path

import pytest

from scripts.ci_lab.open_capture import TREE


def fail_once(node: str, fallback: Path) -> None:
    """Fail the first run of the test `node` on this head and pass every later run.

    Inside the lab the marker sits in the runs folder both attempts share, the parent of the sandbox folder that
    holds TMPDIR; outside the lab it sits in `fallback`, the pytest temp root."""
    tree = os.environ.get(TREE, "")
    folder = Path(os.environ["TMPDIR"]).parent.parent if tree else fallback
    key = hashlib.sha256(f"{Path(tree).name}\n{node}".encode()).hexdigest()
    try:
        (folder / f"fail-once-{key}").touch(exist_ok=False)
    except FileExistsError:
        return
    pytest.fail(f"fail once: the first run of {node} fails")
