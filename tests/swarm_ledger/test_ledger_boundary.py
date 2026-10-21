import ast
import re
from pathlib import Path

import pytest

from tests.swarm_ledger.test_storage_composition import dotted, imported

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "scripts" / "swarm_ledger"
ADAPTERS = LEDGER / "adapters"
FOREIGN = re.compile(r"^(scripts\.(swarm|swarm_v2|hive)|hooks)(\.|$)")


def loaded(path: Path) -> set:
    return {
        dotted(node)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and dotted(node)
    }


def foreign(path: Path, root: Path = ROOT) -> list:
    return sorted(name for name in imported(path, root) | loaded(path) if FOREIGN.match(name))


def offenders() -> list:
    sources = [path for path in LEDGER.rglob("*.py") if ADAPTERS not in path.parents]
    return sorted(str(path.relative_to(ROOT)) for path in sources if foreign(path))


def test_the_ledger_imports_no_swarm_hive_or_hooks_module_outside_its_adapters():
    assert offenders() == []


def test_the_adapters_are_where_the_ledger_meets_swarm_hive_and_hooks():
    assert {path.stem for path in ADAPTERS.glob("*.py")} >= {"swarm", "hive", "hooks"}
    assert all(foreign(ADAPTERS / f"{name}.py") for name in ("swarm", "hive", "hooks"))


@pytest.mark.parametrize(
    "source",
    [
        "from scripts.swarm import naming\n",
        "from scripts.swarm.store import SwarmError\n",
        "from scripts.swarm_v2 import front_door\n",
        "import scripts.hive.auth as auth\n",
        "from hooks.context import operator_words\n",
        "import hooks\n",
        'importlib.import_module("hooks.classifier")\n',
    ],
)
def test_the_boundary_check_sees_every_spelling_of_a_foreign_import(tmp_path, source):
    probe = tmp_path / "scripts" / "swarm_ledger" / "probe.py"
    probe.parent.mkdir(parents=True)
    probe.write_text(source, encoding="utf-8")
    assert foreign(probe, tmp_path)


@pytest.mark.parametrize(
    "source",
    [
        "from scripts.swarm_ledger import ledger_core\n",
        "import scripts.swarm_ledger.ports\n",
        "from scripts.swarmish import thing\n",
        "import hookshot\n",
    ],
)
def test_the_boundary_check_lets_ledger_and_lookalike_imports_pass(tmp_path, source):
    probe = tmp_path / "scripts" / "swarm_ledger" / "probe.py"
    probe.parent.mkdir(parents=True)
    probe.write_text(source, encoding="utf-8")
    assert foreign(probe, tmp_path) == []
