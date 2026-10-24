import json
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
REPLAYS = ("tests/swarm_ledger/repository_replay.py", "tests/swarm_ledger/page_replay.py")
# Runs every import statement of the replays after their own sys.path setup, then lists what loaded.
LOADER = """
import ast, json, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path[:0] = [str(root / "scripts/swarm_ledger"), str(root)]
before = set(sys.modules)
for replay in sys.argv[2:]:
    for node in ast.walk(ast.parse((root / replay).read_text())):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            try:
                exec(compile(ast.Module([node], []), replay, "exec"), {})
            except ModuleNotFoundError as error:
                if error.name.split(".")[0] in ("scripts", "hooks", "tests"):
                    raise
loaded = [m for name, m in list(sys.modules.items()) if name not in before]
print(json.dumps(sorted({m.__file__ for m in loaded if getattr(m, "__file__", None)})))
"""


def test_every_repository_module_the_replays_load_triggers_the_workflow(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LEDGER_DIR": str(tmp_path / "ledgers")}
    result = subprocess.run(
        [sys.executable, "-c", LOADER, str(ROOT), *REPLAYS],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    loaded = {Path(name).resolve() for name in json.loads(result.stdout.splitlines()[-1])}
    repository = {
        path.relative_to(ROOT).as_posix()
        for path in loaded
        if path.is_relative_to(ROOT) and "site-packages" not in path.parts
    }
    workflow = yaml.safe_load((ROOT / ".github/workflows/equivalence.yml").read_text())
    paths = workflow[True]["pull_request"].get("paths", ["**"])
    assert sorted(path for path in repository if not any(fnmatch(path, glob) for glob in paths)) == []
