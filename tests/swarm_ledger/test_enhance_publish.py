"""An enhancement planner's publish into a phase that already holds a plan, driven through the live ledger API server."""

import json
from pathlib import Path
from typing import Any

import pytest

from tests.swarm_ledger import test_api_v1
from tests.swarm_ledger.test_api_v1 import SLUG

authority_live = test_api_v1.authority_live
live = test_api_v1.live

PLANNER = "planner@323133-0259"
BASE = "# Plan\n<!-- slice: one -->\n## One\nFirst step\n<!-- slice: two -->\n## Two\nSecond step\n"
SECTION = "# Plan for task t3\n<!-- slice: t3-plan -->\n## Enhance task t3\nScope of the thin task\n"
SEED = [
    {"op": "join", "id": "enhance-master-join", "by": "boss", "role": "orchestrator"},
    {"op": "join", "id": "enhance-planner-join", "by": PLANNER},
]
ROWS = [
    {"op": "task_update", "id": "enhance-done-seed", "by": "swarm", "item": "tasks/t1", "fields": {"state": "done"}},
    {
        "op": "task_add",
        "id": "enhance-target-seed",
        "by": "swarm",
        "task": "t3",
        "title": "Thin task",
        "lane": "eng",
        "phase": "p1",
        "description": "Thin.",
    },
    {
        "op": "task_add",
        "id": "enhance-plan-seed",
        "by": "swarm",
        "task": "enhance-t3",
        "title": "Plan the thin task",
        "lane": "plan",
        "kind": "plan",
        "phase": "p1",
        "enhances": "t3",
    },
    {
        "op": "task_update",
        "id": "enhance-claim-seed",
        "by": "swarm",
        "item": "tasks/enhance-t3",
        "fields": {"claimed_by": PLANNER, "state": "claimed"},
    },
]


def item(ledger: Any, path: str) -> dict[str, Any]:
    found: dict[str, Any] = ledger.resource(SLUG, path)
    return found


def held(ledger: Any) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    phase = item(ledger, "phases/p1")
    tasks = [item(ledger, f"tasks/{task}") for task in ("t1", "t2")]
    slices = ledger.resource(SLUG, "slices", collection=True)
    return (
        {key: phase.get(key) for key in ("plan", "plan_url", "plan_ref")},
        [{key: task.get(key) for key in ("slice", "plan_slice", "plan_lines", "plan_url")} for task in tasks],
        slices,
    )


@pytest.mark.feature("enhance-publish-keeps-phase-plan")
def test_an_enhancement_publish_into_a_planned_phase_keeps_the_phase_plan_and_sibling_slices(
    live: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm_ledger import ledger_artifacts, ledger_core, plan_read
    from scripts.swarm_ledger.api.client import ResourceClient
    from tests.swarm_ledger.test_ledger_authority import core, ledger

    client = ResourceClient(ledger.BASE, ledger.credentials(SLUG, service=True))
    monkeypatch.setattr(ledger_core, "LEDGER_DIR", core.LEDGER_DIR)
    monkeypatch.setattr(
        ledger,
        "upload_artifact",
        lambda slug, name, path, request: ledger_artifacts.store(SLUG, "plan.md", Path(path).read_bytes()),
    )
    monkeypatch.setattr(
        ledger.ledger_publish,
        "publish",
        lambda path, title, repo, artifact, issue_title: (artifact(path, title), "artifact"),
    )
    for name in ("AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM", "AGENTIHOOKS_SWARM_TASK"):
        monkeypatch.delenv(name, raising=False)
    parse = ledger.build_parser().parse_args
    assert [client.mutate(SLUG, [op]).get("rejected") for op in SEED] == [[], []]
    base, section = tmp_path / "plan.md", tmp_path / "enhance.md"
    base.write_text(BASE, encoding="utf-8")
    section.write_text(SECTION, encoding="utf-8")
    ledger.cmd_publish_plan(parse(["--slug", SLUG, "--as", "boss", "publish-plan", str(base), "--phase", "p1"]))
    for task, anchor in (("t1", "one"), ("t2", "two")):
        add = ["task", "add", task, f"Step {anchor}", "--lane", "eng", "--phase", "p1", "--plan-slice", anchor]
        ledger.cmd_task(parse(["--slug", SLUG, "--as", "boss", *add]))
    assert [client.mutate(SLUG, [op]).get("rejected") for op in ROWS] == [[], [], [], []]
    before = held(ledger)
    capsys.readouterr()
    ledger.cmd_publish_plan(parse(["--slug", SLUG, "--as", PLANNER, "publish-plan", str(section), "--phase", "p1"]))
    printed = json.loads(capsys.readouterr().out)
    assert held(ledger) == before
    target = item(ledger, "tasks/t3")
    assert {key: target.get(key) for key in ("plan_url", "plan_slice", "plan_lines")} == {
        "plan_url": printed["plan_url"],
        "plan_slice": "t3-plan",
        "plan_lines": "2-4",
    }
    assert plan_read.main(["read", "--slug", SLUG, "--task", "t3"], environ={}) == 0
    assert capsys.readouterr().out == SECTION
    point = ["task", "set", "t3", f"plan_url={printed['plan_url']}", "plan_slice=t3-plan"]
    ledger.cmd_task(parse(["--slug", SLUG, "--as", PLANNER, *point]))
    assert item(ledger, "tasks/t3")["plan_lines"] == "2-4"
    assert held(ledger) == before
