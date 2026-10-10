import ast
import hashlib
import json
from pathlib import Path

import pytest

from scripts.swarm_v2 import architecture, records, validate_plan
from scripts.swarm_v2.runtime.commands import Principal, Role

ROOT = Path(__file__).resolve().parents[1]
OPERATOR = {
    "slug": "rig",
    "credential": "page",
    "authenticate": lambda slug, credential: Principal("nestor", Role.OPERATOR),
}


def _observe(module, path, calls):
    outputs = []
    for function, arguments, *keywords in calls:
        try:
            result = getattr(module, function)(*arguments, **(keywords[0] if keywords else {}))
        except ValueError as error:
            result = {"error": type(error).__name__, "message": str(error)}
        outputs.append(
            {
                "result": result,
                "bytes": hashlib.sha256(path.read_bytes()).hexdigest(),
                "files": sorted(file.name for file in path.parent.glob(path.name + "*")),
            }
        )
    return outputs


def _replay(module, name, path):
    initial = (ROOT / "docs/swarm-v2" / f"{name}.json").read_text()
    path.write_text(initial)
    path.with_name(path.name + ".tmp").write_text("interrupted write")
    if name == "architecture":
        change = json.loads((ROOT / "tests/fixtures/swarm_v2/architecture/inventory.json").read_text())
        change["base_revision"] = json.loads(initial)["revision"]
        changed = {**change, "base_revision": 999}
        calls = [
            ("apply_inventory", (path, change)),
            ("apply_inventory", (path, dict(reversed(list(change.items()))))),
            ("apply_inventory", (path, changed)),
            ("rollback", (path, change["base_revision"], "rollback"), OPERATOR),
            ("rollback", (path, change["base_revision"], "rollback"), OPERATOR),
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
def test_record_apis_match_the_base_and_detect_a_planted_digest_fault(name, module, tmp_path, monkeypatch):
    expected = json.loads((ROOT / "tests/fixtures/swarm_v2/records/replay.json").read_text())["outputs"][name]
    path = tmp_path / "record.json"
    assert _replay(module, name, path) == expected
    source = Path(records.__file__).read_text().replace("sort_keys=True", "sort_keys=False")
    digest = next(
        node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "digest"
    )
    digest.decorator_list = []
    faulty_helpers = records.__dict__.copy()
    exec(compile(ast.Module(body=[digest], type_ignores=[]), "faulty_digest", "exec"), faulty_helpers)
    monkeypatch.setattr(module, "digest", faulty_helpers["digest"])
    assert _replay(module, name, path) != expected
