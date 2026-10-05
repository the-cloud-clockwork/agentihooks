import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
IMPORT_ALL = """
import importlib, os, sys
for name in sys.argv[1:]:
    importlib.import_module(f"tests.swarm_ledger.{name}")
print(os.environ.get("LEDGER_DIR"))
"""


def test_importing_the_ledger_test_modules_creates_no_ledger_folder(tmp_path):
    tmp, home = tmp_path / "tmp", tmp_path / "home"
    tmp.mkdir()
    home.mkdir()
    env = {key: value for key, value in os.environ.items() if key != "LEDGER_DIR"}
    env.update(TMPDIR=str(tmp), HOME=str(home))
    modules = sorted(path.stem for path in HERE.glob("test_*.py") if path.stem != Path(__file__).stem)
    run = subprocess.run(
        [sys.executable, "-c", IMPORT_ALL, *modules], cwd=ROOT, env=env, capture_output=True, text=True, check=True
    )
    assert run.stdout.strip() == "None"
    assert list(tmp.iterdir()) == []
    assert list(home.iterdir()) == []


def test_each_module_works_in_its_own_temporary_ledger_folder(ledger_dir):
    import ledger_core as core

    assert os.environ["LEDGER_DIR"] == str(ledger_dir)
    assert core.LEDGER_DIR == ledger_dir
    assert Path.home() / "development-ledger" not in (ledger_dir, *ledger_dir.parents)
