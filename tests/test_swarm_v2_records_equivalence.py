import hashlib
import json
from pathlib import Path
from types import FunctionType, ModuleType

import pytest

from scripts.swarm_v2 import architecture, records, validate_plan

ROOT = Path(__file__).resolve().parents[1]


def _observe(module, path, calls):
    outputs = []
    for function, arguments in calls:
        try:
            result = getattr(module, function)(*arguments)
        except ValueError as error:
            result = {"error": type(error).__name__, "message": str(error)}
        outputs.append({"result": result, "bytes": hashlib.sha256(path.read_bytes()).hexdigest()})
    return outputs


def _replay(module, name, path):
    initial = (ROOT / "docs/swarm-v2" / f"{name}.json").read_text()
    path.write_text(initial)
    if name == "architecture":
        change = json.loads((ROOT / "tests/fixtures/swarm_v2/architecture/inventory.json").read_text())
        change["base_revision"] = json.loads(initial)["revision"]
        changed = {**change, "base_revision": 999}
        calls = [
            ("apply_inventory", (path, change)),
            ("apply_inventory", (path, dict(reversed(list(change.items()))))),
            ("apply_inventory", (path, changed)),
            ("rollback", (path, change["base_revision"], "rollback")),
            ("rollback", (path, change["base_revision"], "rollback")),
        ]
    else:
        plan = module.load_plan(ROOT / "Swarm-v2.md")
        change = {
            "operation": "replay",
            "base_revision": json.loads(initial)["revision"],
            "package": "SV2-FND-04",
            "evidence": [],
        }
        backup = path.with_name("backup.json")
        backup.write_text(initial)
        calls = [
            ("record", (path, change, plan, ROOT)),
            ("record", (path, dict(reversed(list(change.items()))), plan, ROOT)),
            ("record", (path, {**change, "base_revision": 999}, plan, ROOT)),
            ("restore", (path, backup, "restore", plan, ROOT)),
            ("reopen", (path, "SV2-FND-04", "reopen")),
        ]
    return _observe(module, path, calls)


@pytest.mark.parametrize("name,module", [("architecture", architecture), ("evidence-index", validate_plan)])
def test_record_apis_match_the_base_and_detect_a_planted_digest_fault(name, module, tmp_path):
    expected = json.loads((ROOT / "tests/fixtures/swarm_v2/records/replay.json").read_text())["outputs"][name]
    path = tmp_path / "record.json"
    assert _replay(module, name, path) == expected
    planted = ModuleType("planted")
    planted.__dict__.update(module.__dict__)
    faulty_helpers = ModuleType("faulty_helpers")
    source = Path(records.__file__).read_text().replace("sort_keys=True", "sort_keys=False")
    exec(compile(source, "faulty_helpers", "exec"), faulty_helpers.__dict__)
    planted.digest = faulty_helpers.digest
    for function, value in module.__dict__.items():
        if callable(value) and getattr(value, "__globals__", None) is module.__dict__:
            planted.__dict__[function] = FunctionType(value.__code__, planted.__dict__, function, value.__defaults__)
    assert _replay(planted, name, path) != expected
