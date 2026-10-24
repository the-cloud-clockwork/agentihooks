import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import ci_equivalence_trigger as trigger

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
# Runs every import statement and patch target of the replays after their own sys.path setup, then lists what loaded.
LOADER = """
import ast, importlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path[:0] = [str(root / "scripts/swarm_ledger"), str(root)]
from scripts import ci_equivalence_trigger as trigger
before = set(sys.modules)
for replay in trigger.REPLAYS:
    text = (root / replay).read_text()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            try:
                exec(compile(ast.Module([node], []), replay, "exec"), {})
            except ModuleNotFoundError as error:
                if trigger.module_files(root, error.name):
                    raise
    for name in trigger.patched_names(text):
        parts = name.split(".")
        for depth in range(len(parts), 0, -1):
            try:
                importlib.import_module(".".join(parts[:depth]))
                break
            except ModuleNotFoundError:
                pass
loaded = [m for name, m in list(sys.modules.items()) if name not in before]
print(json.dumps(sorted({m.__file__ for m in loaded if getattr(m, "__file__", None)})))
"""


def write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def test_every_repository_module_the_replays_load_is_in_the_trigger(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LEDGER_DIR": str(tmp_path / "ledgers")}
    result = subprocess.run(
        [sys.executable, "-c", LOADER, str(ROOT)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    loaded = {Path(name).resolve() for name in json.loads(result.stdout.splitlines()[-1])}
    repository = {
        path.relative_to(ROOT).as_posix()
        for path in loaded
        if path.is_relative_to(ROOT) and "site-packages" not in path.parts
    }
    assert {"scripts/inbox/store.py", "scripts/swarm/store.py", "hooks/_redis.py"} <= repository
    assert sorted(repository - trigger.imported_files(ROOT)) == []


@pytest.fixture
def replays(tmp_path):
    write(
        tmp_path,
        {
            trigger.REPLAYS[0]: "import ledger_core\nfrom scripts.inbox import store\n",
            trigger.REPLAYS[1]: "def run():\n    with patch('hooks.redis_link.get', None):\n        import page\n",
            "scripts/__init__.py": "",
            "scripts/swarm_ledger/ledger_core.py": "from scripts.gates import talk\n",
            "scripts/swarm_ledger/page.py": "",
            "scripts/inbox/__init__.py": "",
            "scripts/inbox/store.py": "from .keys import KEY\n",
            "scripts/inbox/keys.py": "KEY = 1\n",
            "scripts/gates/__init__.py": "",
            "scripts/gates/talk.py": "def budget():\n    from scripts.swarm.plan import steps\n",
            "scripts/swarm/__init__.py": "",
            "scripts/swarm/plan.py": "",
            "hooks/__init__.py": "",
            "hooks/redis_link.py": "",
            "scripts/unrelated.py": "",
        },
    )
    return tmp_path


def test_the_trigger_follows_relative_nested_and_patched_imports(replays):
    files = trigger.imported_files(replays)
    assert {
        "scripts/swarm_ledger/ledger_core.py",
        "scripts/inbox/store.py",
        "scripts/inbox/keys.py",
        "scripts/gates/talk.py",
        "scripts/swarm/plan.py",
        "scripts/swarm/__init__.py",
        "hooks/redis_link.py",
        "hooks/__init__.py",
    } <= files
    assert "scripts/unrelated.py" not in files


@pytest.mark.parametrize(
    ("changed", "hits"),
    [
        (["scripts/inbox/keys.py"], ["scripts/inbox/keys.py"]),
        (["scripts/swarm/plan.py", "scripts/unrelated.py"], ["scripts/swarm/plan.py"]),
        (["scripts/swarm_ledger/static/app.js"], ["scripts/swarm_ledger/static/app.js"]),
        ([".github/workflows/equivalence.yml"], [".github/workflows/equivalence.yml"]),
        (["scripts/unrelated.py", "docs/page.md"], []),
    ],
)
def test_a_change_triggers_only_when_it_reaches_the_replays(replays, changed, hits):
    assert trigger.triggering(changed, trigger.imported_files(replays)) == hits


@pytest.mark.parametrize(("touched", "run"), [("scripts/inbox/keys.py", "true"), ("scripts/unrelated.py", "false")])
def test_main_writes_whether_the_pull_request_reaches_the_replays(replays, monkeypatch, touched, run):
    git(replays, "init", "-q", "-b", "dev")
    git(replays, "add", ".")
    git(replays, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    base = git(replays, "rev-parse", "HEAD")
    (replays / touched).write_text("CHANGED = 1\n")
    git(replays, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "head")
    output = replays / "output"
    monkeypatch.chdir(replays)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert trigger.main(["--base", base, "--head", "HEAD"]) == 0
    assert output.read_text() == f"run={run}\n"
