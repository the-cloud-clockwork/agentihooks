from pathlib import Path

from scripts import size_limits

ROOT = Path(__file__).resolve().parents[2]
UNITS = (
    "scripts/swarm_ledger/ledger.py::cmd_task",
    "scripts/swarm_ledger/ledger_agent_ops.py::check",
    "scripts/swarm_ledger/ledger_tasks.py::_update",
)


def test_the_split_ledger_units_meet_the_complexity_limits():
    paths = {ROOT / unit.split("::")[0] for unit in UNITS}
    measured = {}
    size_limits._ruff_hits(ROOT, {path: size_limits.functions(path.read_text()) for path in paths}, measured)
    assert measured == {}


def test_the_split_ledger_units_left_the_allowlist():
    assert [unit for unit in UNITS if unit in size_limits.load(ROOT)] == []
