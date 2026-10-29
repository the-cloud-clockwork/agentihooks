import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROBE = """import hashlib
import json
import os
import sys
from pathlib import Path

from mutmut import __main__ as runner
from scripts.ci_mutation.selection import collect_shard_stats, keep_node_ids, selected_mutants

root = Path.cwd()
keep_node_ids()
code, names = selected_mutants("scripts/sample.py", "def value():\\n    return 1\\n", {2})
(root / "mutants/scripts/sample.py").write_text(code)
mutant = "scripts.sample." + names[0]
order = ["tests/test_sample.py::test_" + name for name in sys.argv[1:]]
test_runner = runner.PytestRunner()
sys.path.insert(0, str(root / "mutants"))
for name in list(sys.modules):
    if name == "scripts" or name.startswith("scripts."):
        sys.modules.pop(name)
collect_shard_stats(runner, test_runner, order, root / "stats.json", str(root / "basetemp"))
stats = json.loads((root / "stats.json").read_text())
assert stats["status"] == 0, stats
credited = stats["tests"]["scripts.sample.x_value"]
os.environ.pop("MUTANT_UNDER_TEST", None)
status = test_runner.run_tests(mutant_name=mutant, tests=credited)
control = test_runner.run_tests(mutant_name=mutant, tests=["tests/test_sample.py::test_strong"])
result = {
    "mutant": mutant,
    "source_sha256": hashlib.sha256(code.encode()).hexdigest(),
    "order": order,
    "credited": credited,
    "status": status,
    "strong_control": control,
}
(root / "result.json").write_text(json.dumps(result))
print(json.dumps(result), flush=True)
"""


def test_session_fixture_credit_changes_with_the_first_consumer(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv("MUTATION_STATS_CACHE", raising=False)
    monkeypatch.delenv("MUTANT_UNDER_TEST", raising=False)
    results = []
    for order in (("weak", "strong"), ("strong", "weak")):
        root = tmp_path / order[0]
        (root / "scripts").mkdir(parents=True)
        (root / "scripts/__init__.py").touch()
        shutil.copytree(
            Path(__file__).parents[1] / "scripts/ci_mutation",
            root / "scripts/ci_mutation",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        (root / "mutants/scripts").mkdir(parents=True)
        (root / "mutants/scripts/__init__.py").touch()
        (root / "mutants/tests").mkdir()
        (root / "mutants/tests/test_sample.py").write_text(
            "import pytest\nfrom scripts.sample import value\n\n"
            '@pytest.fixture(scope="session")\n'
            "def shared():\n    return value()\n\n"
            "def test_weak(shared):\n    assert shared is not None\n\n"
            "def test_strong(shared):\n    assert shared == 1\n"
        )
        config = '[tool.mutmut]\nsource_paths = ["scripts/"]\n[tool.pytest.ini_options]\n'
        (root / "pyproject.toml").write_text(config)
        (root / "mutants/pyproject.toml").write_text(config)
        (root / "probe.py").write_text(PROBE)
        done = subprocess.run(
            [sys.executable, "probe.py", *order], cwd=root, capture_output=True, text=True, timeout=90
        )
        print(done.stdout, done.stderr, flush=True)
        assert done.returncode == 0, done.stdout + done.stderr
        results.append(json.loads((root / "result.json").read_text()))
    folder = Path(os.environ.get("PROOF_REPORT_DIR", tmp_path / "reports"))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "mutation-credit.json").write_text(json.dumps(results, indent=2))
    before, after = results
    assert before["mutant"] == after["mutant"]
    assert before["source_sha256"] == after["source_sha256"]
    assert before["credited"] == ["tests/test_sample.py::test_weak"]
    assert after["credited"] == ["tests/test_sample.py::test_strong"]
    assert before["status"] == 0
    assert after["status"] == 1
    assert before["strong_control"] == after["strong_control"] == 1
