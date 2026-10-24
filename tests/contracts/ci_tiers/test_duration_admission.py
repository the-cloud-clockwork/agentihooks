import copy
import json
from datetime import date
from pathlib import Path

import pytest

from scripts import ci_reuse, ci_wiring, gate_protected

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]


def legacy(workflows):
    base = copy.deepcopy(workflows)
    jobs = base["test.yml"]["jobs"]
    if "durations" in jobs:
        return base
    selection = jobs["select"]
    end = next(index + 1 for index, step in enumerate(selection["steps"]) if step.get("id") == "republished")
    durations = copy.deepcopy(selection)
    durations["steps"] = durations["steps"][:end]
    durations["outputs"] = {name: selection["outputs"].pop(name) for name in ("key", "queued", "base", "source")}
    durations["timeout-minutes"] = (
        "${{ (github.event_name == 'merge_group' || github.event_name == 'workflow_dispatch') && 4 || 2 }}"
    )
    selection["steps"] = selection["steps"][end:]
    selection["if"] = selection["if"].replace("!cancelled()", "!cancelled() && github.event_name != 'merge_group'")
    jobs["durations"] = durations
    for job in jobs.values():
        encoded = json.dumps(job)
        for name in durations["outputs"]:
            encoded = encoded.replace(f"needs.select.outputs.{name}", f"needs.durations.outputs.{name}")
        if encoded != json.dumps(job):
            job.update(json.loads(encoded))
            job["needs"] = list(dict.fromkeys(job["needs"] + ["durations"]))
    jobs["gate-required"]["needs"].append("durations")
    jobs["gate-required"]["steps"] = ci_reuse.gate_steps(
        queue_tier=True, promotion=True, docs="docs" in jobs["gate-required"]["needs"]
    )
    return base


def combined(base, promotion=True):
    head = copy.deepcopy(base)
    jobs = head["test.yml"]["jobs"]
    durations = jobs.pop("durations")
    selection = jobs["select"]
    selection["outputs"] = durations["outputs"] | selection["outputs"]
    selection["steps"] = durations["steps"] + selection["steps"]
    selection["if"] = durations["if"]
    for job in jobs.values():
        if isinstance(job.get("needs"), list):
            job["needs"] = list(dict.fromkeys("select" if name == "durations" else name for name in job["needs"]))
    head["test.yml"]["jobs"] = json.loads(json.dumps(jobs).replace("needs.durations.outputs.", "needs.select.outputs."))
    head["test.yml"]["jobs"]["gate-required"]["steps"] = ci_reuse.gate_steps(
        queue_tier=True, promotion=promotion, docs="docs" in jobs["gate-required"]["needs"]
    )
    step = head["test.yml"]["jobs"]["gate-required"]["steps"][0]
    step["run"] = (
        step["run"]
        .replace('["durations", "split",', '["select", "split",')
        .replace('["durations", "select",', '["select",')
    )
    return head


def grade(base, head):
    config = json.loads((ROOT / ci_wiring.CONFIG).read_text())
    policy = json.loads((ROOT / ".github/workflows/trusted-actors.json").read_text())
    return gate_protected.grade(base, config, head, config, date(2026, 10, 16), policy)


def test_protected_grading_accepts_separate_duration_admission():
    base = legacy(ci_wiring.load(ROOT))
    assert grade(base, copy.deepcopy(base)) == []


@pytest.mark.parametrize("already_combined", [False, True])
@pytest.mark.parametrize("promotion", [False, True])
def test_protected_grading_accepts_combined_duration_admission(already_combined, promotion):
    base = legacy(ci_wiring.load(ROOT))
    head = combined(base, promotion)
    if already_combined:
        base = copy.deepcopy(head)
    assert grade(base, head) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("env", {"PYTHONPATH": "/untrusted"}),
        ("defaults", {"run": {"working-directory": "/untrusted"}}),
        ("permissions", {"contents": "write"}),
        ("timeout-minutes", 60),
        ("outputs", {"key": "forged"}),
    ],
)
def test_combined_admission_keeps_its_protected_execution_context(field, value):
    base = legacy(ci_wiring.load(ROOT))
    head = combined(base)
    head["test.yml"]["jobs"]["select"][field] = value
    assert grade(base, head)


def test_combined_admission_keeps_provenance_before_head_execution():
    base = legacy(ci_wiring.load(ROOT))
    head = combined(base)
    head["test.yml"]["jobs"]["select"]["steps"].insert(0, {"run": "echo forged"})
    problems = grade(base, head)
    assert any("provenance" in problem for problem in problems)
    assert any("base need durations" in problem for problem in problems)


def test_combined_admission_cannot_retire_another_required_gate():
    base = legacy(ci_wiring.load(ROOT))
    head = combined(base)
    head["test.yml"]["jobs"]["gate-required"]["needs"].remove("lint")
    assert any("base need lint" in problem for problem in grade(base, head))


def test_combined_admission_requires_an_existing_selection_job():
    base = legacy(ci_wiring.load(ROOT))
    head = combined(base)
    del base["test.yml"]["jobs"]["select"]
    assert any("existing selection" in problem for problem in grade(base, head))


@pytest.mark.parametrize("missing", ["base", "head", "both"])
def test_missing_all_provenance_producers_is_reported(missing):
    base = legacy(ci_wiring.load(ROOT))
    head = combined(base)
    for workflows in [base] if missing == "base" else [head] if missing == "head" else [base, head]:
        workflows["test.yml"]["jobs"].pop("durations", None)
        workflows["test.yml"]["jobs"].pop("select", None)
    assert any("provenance" in problem for problem in grade(base, head))


def test_missing_base_duration_outputs_is_reported():
    base = legacy(ci_wiring.load(ROOT))
    head = combined(base)
    del base["test.yml"]["jobs"]["durations"]["outputs"]
    assert any("provenance context outputs" in problem for problem in grade(base, head))


def test_separate_duration_producer_cannot_be_unhooked_from_the_gate():
    base = legacy(ci_wiring.load(ROOT))
    head = copy.deepcopy(base)
    head["test.yml"]["jobs"]["gate-required"]["needs"].remove("durations")
    assert any("base need durations" in problem for problem in grade(base, head))


def test_missing_reuse_cannot_retire_the_duration_dependency():
    base = legacy(ci_wiring.load(ROOT))
    head = combined(base)
    del head["test.yml"]["jobs"]["reuse"]
    problems = grade(base, head)
    assert "The head removes the protected reuse job." in problems
    assert any("base need durations" in problem for problem in problems)
