import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.ci_mutation.runner import prepare_workspace, run_gate

PATHS = [
    "scripts/swarm_ledger/api/mutations.py",
    "scripts/swarm_ledger/events/hub.py",
    "scripts/swarm_ledger/events/patch.py",
    "scripts/swarm_ledger/events/publishing.py",
    "scripts/swarm_ledger/ledger_server.py",
    "scripts/swarm_ledger/repository/__init__.py",
    "scripts/swarm_ledger/repository/fragments.py",
    "scripts/swarm_ledger/repository/rows.py",
    "scripts/swarm_ledger/repository/sqlite.py",
]
CASE = "tests/swarm_ledger/receipt_tracing_case.py"
NODE = f"{CASE}::test_latest_thousand_operation_receipts_remain_retry_safe"


def statistics(root, work, baseline=False, delay=False):
    work.mkdir()
    paths = [path for path in PATHS if (root / path).is_file()]
    prepare_workspace(root, work, paths, [CASE])
    (work / CASE).write_text(
        "from tests.swarm_ledger.test_api_v1 import authority_live, live, "
        "test_latest_thousand_operation_receipts_remain_retry_safe\n"
    )
    if delay:
        (work / CASE).write_text(
            "import time\n"
            "from tests.swarm_ledger.test_api_v1 import authority_live, live, server, "
            "test_latest_thousand_operation_receipts_remain_retry_safe as receipt_case\n\n"
            "def test_latest_thousand_operation_receipts_remain_retry_safe(live, monkeypatch):\n"
            "    post = server.Handler.do_POST\n"
            "    def delayed_post(handler):\n"
            "        time.sleep(6)\n"
            "        return post(handler)\n"
            "    monkeypatch.setattr(server.Handler, 'do_POST', delayed_post)\n"
            "    receipt_case(live)\n"
        )
    if baseline:
        factor = work / "tests/mutation_trace.py"
        factor.write_text(factor.read_text().replace("MUTATION_TRACE_FACTOR = 7", "MUTATION_TRACE_FACTOR = 1"))
    selection = work / "selection.json"
    selection.write_text(
        json.dumps(
            {
                path: {"lines": list(range(1, len((root / path).read_text().splitlines()) + 1)), "tests": [CASE]}
                for path in paths
            }
        )
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.ci_mutation.selection",
            str(selection),
            "0",
            "1",
            "collect",
            str(work / "stats.json"),
            "receipt-stats",
            "0",
            "1",
        ],
        cwd=work,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    (work / "stats.log").write_text(result.stdout)
    print(result.stdout, flush=True)
    if baseline:
        assert result.returncode != 0 and "TimeoutError: timed out" in result.stdout
        print("Original five second deadline control failed on receipt POST with six second tracing delay.", flush=True)
        return
    assert result.returncode == 0, f"receipt mutation statistics failed with {result.returncode}"
    stats = json.loads((work / "stats.json").read_text())
    assert stats["results"] and all(result["status"] == 0 for result in stats["results"])
    assert any(NODE in tests for result in stats["results"] for tests in result["tests"].values())
    print(json.dumps(stats), flush=True)


def missing_coverage(root, work):
    path = "scripts/swarm_ledger/repository/rows.py"
    source = root / path
    line = next(
        index + 1 for index, text in enumerate(source.read_text().splitlines()) if "return json.dumps(value," in text
    )
    prepare_workspace(root, work, [path], ["tests/test_receipt_covering.py"])
    (work / "tests").rename(work / "removed-covering-tests")
    (work / "tests").mkdir()
    (work / "scripts/coverage_control.py").write_text("def covered(value):\n    return value + 1\n")
    (work / "tests/test_receipt_covering.py").write_text(
        "from scripts.swarm_ledger.repository import rows\n"
        "from scripts.coverage_control import covered\n\n"
        "def test_covering():\n    assert rows.__name__.endswith('rows')\n    assert covered(1) == 2\n"
    )
    report = run_gate(work, {path: {line}, "scripts/coverage_control.py": {2}}, work / "evidence", 120)
    print(json.dumps(report), flush=True)
    assert report["failed"]
    assert report["not_mutated"] == []
    failures = report["files"][0]["failures"]
    assert failures
    assert all(row["status"] == "no tests" for row in failures)
    assert report["files"][0]["counts"] == {"no tests": len(failures)}


if __name__ == "__main__":
    root = Path.cwd()
    work = root / ".receipt-mutation-proof"
    if sys.argv[1] in ("stats", "stats-baseline", "stats-compensated"):
        statistics(root, work, sys.argv[1] == "stats-baseline", sys.argv[1] != "stats")
    else:
        missing_coverage(root, work)
