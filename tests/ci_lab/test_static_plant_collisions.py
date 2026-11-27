import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import ci_wiring, size_limits, type_gate
from scripts.ci_lab.static_gates import STATIC_JOBS

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
PLANTS = ROOT / "tests" / "ci_lab" / "plants"
WIRING_PLANT = "lint.check-every-pull-request-job-is-a-need-of-gate-required"


def _load_plant(name: str) -> dict[str, Any]:
    return json.loads((PLANTS / f"{name}.json").read_text())


def _qualifying_type_plants() -> dict[str, dict[str, Any]]:
    found = {path.stem: json.loads(path.read_text()) for path in sorted(PLANTS.glob("*.json"))}
    out: dict[str, dict[str, Any]] = {}
    for name, spec in found.items():
        job = spec["gate"].partition("/")[0]
        if spec["gate"] in STATIC_JOBS or job not in STATIC_JOBS or job == "type-gate":
            continue
        edits = spec["edits"]
        if any(edit["path"].endswith(".py") and edit["path"].startswith(("hooks/", "scripts/")) for edit in edits):
            out[name] = spec
    return out


def _rounds(plants: dict[str, dict[str, Any]]) -> list[dict[str, dict[str, Any]]]:
    pending = dict(plants)
    rounds: list[dict[str, dict[str, Any]]] = []
    while pending:
        current: dict[str, dict[str, Any]] = {}
        content: dict[str, str] = {}
        for name, spec in pending.items():
            applied: dict[str, str] = {}
            fits = True
            for edit in spec["edits"]:
                path = edit["path"]
                text = content[path] if path in content else (ROOT / path).read_text()
                if text.count(edit["before"]) != 1:
                    fits = False
                    break
                applied[path] = text.replace(edit["before"], edit["after"])
            if fits:
                current[name] = spec
                content.update(applied)
        assert current, f"no edit of {sorted(pending)} occurs once in this tree"
        rounds.append(current)
        pending = {name: spec for name, spec in pending.items() if name not in current}
    return rounds


def _copy_tree(root: Path, dest: Path) -> None:
    for path in size_limits.files(root):
        name = path.relative_to(root).as_posix()
        if name.startswith(("hooks/", "scripts/", "tests/")):
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    (dest / "pyproject.toml").write_bytes((root / "pyproject.toml").read_bytes())


def _owners(round_plants: dict[str, dict[str, Any]], path: str) -> list[str]:
    return sorted(name for name, spec in round_plants.items() if any(edit["path"] == path for edit in spec["edits"]))


@pytest.mark.feature("lab-static-plants-turn-only-their-own-gate")
def test_every_static_plant_outside_the_type_gate_job_keeps_its_file_s_baseline(tmp_path: Path) -> None:
    baseline = type_gate.load(ROOT) or {}
    failures: list[str] = []
    for index, round_plants in enumerate(_rounds(_qualifying_type_plants())):
        tree = tmp_path / f"round-{index}"
        tree.mkdir()
        _copy_tree(ROOT, tree)
        paths: set[str] = set()
        for spec in round_plants.values():
            for edit in spec["edits"]:
                text = (tree / edit["path"]).read_text()
                assert text.count(edit["before"]) == 1
                (tree / edit["path"]).write_text(text.replace(edit["before"], edit["after"]))
                paths.add(edit["path"])
        counts = type_gate.measure(tree, sorted(paths))
        for path, count in counts.items():
            allowed = baseline.get(path, 0)
            if count > allowed:
                owners = ", ".join(_owners(round_plants, path))
                failures.append(f"{owners} leaves {path} with {count} type errors, more than the {allowed} allowed")
    assert failures == []


def _patched_workflows(edits: list[dict[str, str]]) -> dict[str, Any]:
    workflows = ci_wiring.load(ROOT)
    for edit in edits:
        if edit["path"].startswith(".github/workflows/"):
            name = Path(edit["path"]).name
            text = (ROOT / edit["path"]).read_text()
            assert text.count(edit["before"]) == 1
            workflows[name] = yaml.safe_load(text.replace(edit["before"], edit["after"]))
    return workflows


def _wiring_problems(edits: list[dict[str, str]]) -> list[str]:
    workflows = _patched_workflows(edits)
    config = json.loads((ROOT / ci_wiring.CONFIG).read_text())
    delivery = ROOT / "delivery.yaml"
    lab = yaml.safe_load(delivery.read_text())["lab"] if delivery.is_file() else None
    return ci_wiring.check(workflows, config, lab)


@pytest.mark.feature("lab-static-plants-turn-only-their-own-gate")
def test_the_wiring_plant_reports_its_own_reason() -> None:
    spec = _load_plant(WIRING_PLANT)
    problems = _wiring_problems(spec["edits"])
    diagnostic = spec["reason"].partition(":")[0]
    assert re.search(rf"\b{re.escape(diagnostic)}\b", "\n".join(problems))


@pytest.mark.feature("lab-static-plants-turn-only-their-own-gate")
def test_the_wiring_plant_keeps_the_gate_s_needs() -> None:
    spec = _load_plant(WIRING_PLANT)
    base_needs = ci_wiring.load(ROOT)["test.yml"]["jobs"]["gate-required"]["needs"]
    planted_needs = _patched_workflows(spec["edits"])["test.yml"]["jobs"]["gate-required"]["needs"]
    assert planted_needs == base_needs
