"""General workload: a generated test suite run by pytest with the given number of xdist worker processes."""

import argparse
import subprocess
import sys
from pathlib import Path

MODULES = 40
TESTS = 12
BODY = """import json
import re
import sqlite3


def _rows(seed):
    return [{"id": i, "name": f"row-{seed}-{i}", "tags": [str(i % 7), str(i % 11)]} for i in range(4000)]


"""
TEST = """def test_case_{index}(tmp_path):
    rows = _rows({index})
    text = json.dumps(rows)
    assert len(json.loads(text)) == 4000
    assert len(re.findall(r"row-{index}-[0-9]+", text)) == 4000
    db = sqlite3.connect(tmp_path / "rows.db")
    db.execute("create table r (id integer, name text)")
    db.executemany("insert into r values (?, ?)", [(r["id"], r["name"]) for r in rows])
    assert db.execute("select count(*) from r").fetchone()[0] == 4000
    (tmp_path / "rows.json").write_text(text)
    assert sorted(rows, key=lambda r: r["name"])[0]["id"] == 0


"""


def generate(root: Path) -> Path:
    suite = root / "suite"
    suite.mkdir()
    for module in range(MODULES):
        tests = "".join(TEST.format(index=module * TESTS + i) for i in range(TESTS))
        (suite / f"test_generated_{module}.py").write_text(BODY + tests)
    return suite


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--children", type=int, required=True)
    args = parser.parse_args()
    suite = generate(Path.cwd())
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-n",
        str(args.children),
        "--rootdir",
        str(suite),
    ]
    return subprocess.call([*command, "-c", "/dev/null", "--basetemp", str(Path.cwd() / "pytest-tmp"), str(suite)])


if __name__ == "__main__":
    sys.exit(main())
