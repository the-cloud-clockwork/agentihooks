import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.shards import assign_files, discover_test_files

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent


def test_every_test_file_lands_in_exactly_one_shard():
    files = discover_test_files(_ROOT)
    groups = assign_files({}, files, 4)
    assigned = [path for group in groups for path in group]
    assert sorted(assigned) == files
    assert "tests/test_shards.py" in files
    assert "tests/lifecycle/test_lease.py" in files


def test_shards_balance_the_stored_durations_per_file():
    durations = {
        "tests/test_a.py::t1": 4.0,
        "tests/test_a.py::t2": 2.0,
        "tests/test_b.py::t": 5.0,
        "tests/test_c.py::t": 3.0,
        "tests/test_d.py::t": 3.0,
        "tests/gone.py::t": 9.0,
    }
    files = ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "tests/test_d.py", "tests/test_e.py"]
    groups = assign_files(durations, files, 2)
    assert sorted(map(sorted, groups)) == [
        ["tests/test_a.py", "tests/test_d.py"],
        ["tests/test_b.py", "tests/test_c.py", "tests/test_e.py"],
    ]


def test_shard_option_collects_only_that_shards_files():
    files = discover_test_files(_ROOT)
    shard = 2
    expected = set(assign_files(_stored_durations(), files, 4)[shard - 1])
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q", "-p", "no:xdist", "--shard", f"{shard}/4"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    collected = {line.split("::")[0] for line in result.stdout.splitlines() if "::" in line}
    assert collected and collected <= expected


def _stored_durations():
    return json.loads((_ROOT / ".test_durations").read_text())
