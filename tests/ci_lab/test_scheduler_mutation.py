import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.ci_lab import executors
from scripts.ci_lab.units import Priority
from scripts.ci_mutation.plan import MUTANT_SECONDS
from scripts.dep_manifest import _git
from tests.ci_lab.test_scheduler import (
    ALPHA,
    DELIVERY,
    FIRST,
    GAMMA,
    SUITE_MANIFEST,
    Port,
    _commit,
    _suite,
)
from tests.ci_lab.test_scheduler import staticless as staticless

KEPT = "tests/test_kept.py"
PASSING = "def test_it():\n    pass\n"


@pytest.mark.feature("lab-mutation-units-work-on-the-change")
@pytest.mark.usefixtures("staticless")
def test_a_promotion_head_names_on_its_mutant_units_the_test_files_they_trace(tmp_path: Path) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    tests = {**SUITE_MANIFEST["tests"], f"{KEPT}::test_it": {"units": [1], "files": [], "escapes": False}}
    _git(suite.repo, "switch", "-q", "-c", "other", suite.base)
    base = _commit(suite.repo, {GAMMA: PASSING, KEPT: PASSING, "tests/test_gone.py": PASSING})
    _git(suite.repo, "rm", "-q", "tests/test_gone.py")
    head = _commit(
        suite.repo,
        {
            "scripts/alpha.py": ALPHA.replace("return 1", "return 4"),
            "tests/test_beta.py": "def test_second():\n    assert True\n",
            "tests/helper.py": "HELP = 1\n",
            "tests/unit/test_data.json": "{}\n",
        },
    )
    request = replace(suite.request(Priority.PROMOTION, head), base=base)

    found = Port({**SUITE_MANIFEST, "tests": tests}).expander(tmp_path).expand(request)

    traced = ["tests/test_alpha.py", "tests/test_beta.py", GAMMA]
    stats, mutants = found.gates["mutation-stats"], found.gates["mutation"]
    assert [(unit.kind, unit.members) for unit in stats] == [("stats", (path,)) for path in traced]
    assert [executors.function_of(unit.members[0]) for unit in mutants] == [FIRST]
    assert [json.loads(unit.manifest) for unit in mutants] == [traced]
    assert [unit.manifest for unit in stats] == [""] * 3


@pytest.mark.feature("lab-mutation-units-work-on-the-change")
@pytest.mark.usefixtures("staticless")
def test_every_part_of_a_split_mutant_unit_names_the_test_files_it_traces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    head = _commit(suite.repo, {"scripts/alpha.py": ALPHA.replace("return 1", "return 4 + 1")})
    monkeypatch.setattr(executors, "UNIT_TIMEOUT_S", MUTANT_SECONDS + 1.0)

    mutants = Port(SUITE_MANIFEST).expander(tmp_path).expand(suite.request(Priority.PROMOTION, head)).gates["mutation"]

    assert len(mutants) > 2
    assert [json.loads(unit.manifest) for unit in mutants] == [["tests/test_alpha.py"]] * len(mutants)


@pytest.mark.feature("lab-mutation-units-work-on-the-change")
@pytest.mark.usefixtures("staticless")
def test_a_test_module_the_head_changes_rekeys_the_mutant_units_that_trace_it(tmp_path: Path) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    expander = Port(SUITE_MANIFEST).expander(tmp_path)
    _git(suite.repo, "switch", "-q", "-c", "other", suite.base)
    changed = _commit(suite.repo, {"scripts/alpha.py": ALPHA.replace("return 1", "return 4")})
    weak = _commit(suite.repo, {"tests/test_beta.py": "def test_second():\n    assert True\n"})
    exact = _commit(suite.repo, {"tests/test_beta.py": "def test_second():\n    assert 1 == 1\n"})

    found = [
        expander.expand(suite.request(Priority.PROMOTION, head)).gates["mutation"] for head in (changed, weak, exact)
    ]

    assert [[unit.members for unit in units] for units in found] == [[unit.members for unit in found[0]]] * 3
    assert [json.loads(units[0].manifest) for units in found] == [
        ["tests/test_alpha.py"],
        ["tests/test_alpha.py", "tests/test_beta.py"],
        ["tests/test_alpha.py", "tests/test_beta.py"],
    ]
    assert len({units[0].key for units in found}) == 3


@pytest.mark.feature("lab-mutation-units-work-on-the-change")
@pytest.mark.usefixtures("staticless")
def test_a_promotion_head_that_changes_no_function_plans_no_stats_unit_for_its_test_modules(tmp_path: Path) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    _git(suite.repo, "switch", "-q", "-c", "other", suite.base)
    edits = {"scripts/alpha.py": ALPHA + "\n\nLIMIT = 1\n", GAMMA: PASSING, "tests/test_beta.py": PASSING}
    head = _commit(suite.repo, edits)

    found = Port(SUITE_MANIFEST).expander(tmp_path).expand(suite.request(Priority.PROMOTION, head))

    assert {"mutation-stats", "mutation"}.isdisjoint(found.gates)
