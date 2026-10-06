import copy

import pytest

from scripts.swarm_ledger import new_ledger


def content():
    return {
        "title": "Automatic phase intake",
        "phases": [
            {"title": "Prepare", "planning": "manual"},
            {"title": "Build", "planning": "auto", "depends_on": [1]},
            {"title": "Verify", "planning": "auto", "depends_on": [1, 2]},
        ],
    }


def test_dependency_positions_become_phase_ids_without_mutating_content():
    seed = content()
    original = copy.deepcopy(seed)
    assert new_ledger.check(seed) == []
    doc = new_ledger.build_doc(seed, "swarm")
    assert doc["phases"] == [
        {"id": "p1", "title": "Prepare", "description": "", "comments": [], "done": False, "planning": "manual"},
        {
            "id": "p2",
            "title": "Build",
            "description": "",
            "comments": [],
            "done": False,
            "planning": "auto",
            "depends_on": ["p1"],
        },
        {
            "id": "p3",
            "title": "Verify",
            "description": "",
            "comments": [],
            "done": False,
            "planning": "auto",
            "depends_on": ["p1", "p2"],
        },
    ]
    assert doc["tasks"] == []
    assert seed == original


def test_positions_and_existing_ids_can_mix():
    seed = content()
    seed["phases"][2]["depends_on"] = [1, "p2"]
    assert new_ledger.check(seed) == []
    assert new_ledger.build_doc(seed)["phases"][2]["depends_on"] == ["p1", "p2"]


@pytest.mark.parametrize("dependency", [0, -1, 4, True, False, 1.5, None, {}])
def test_invalid_positions_are_refused(dependency):
    seed = content()
    seed["phases"][1]["depends_on"] = [dependency]
    assert new_ledger.check(seed)


def test_positional_cycle_names_generated_ids():
    seed = content()
    seed["phases"][0]["depends_on"] = [2]
    assert "p1 -> p2 -> p1" in " ".join(new_ledger.check(seed))


def test_manual_tasks_and_absent_phase_fields_keep_their_shape():
    seed = {"title": "Manual", "phases": [{"title": "Prepare"}], "tasks": [{"title": "Build", "phase": "p1"}]}
    doc = new_ledger.build_doc(seed)
    assert "planning" not in doc["phases"][0]
    assert "depends_on" not in doc["phases"][0]
    assert len(doc["tasks"]) == 1
    assert doc["tasks"][0]["phase"] == "p1"
