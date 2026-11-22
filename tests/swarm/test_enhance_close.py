"""Enhancement close: a plan task carrying enhances closes only on a complete plan section for its target, and the
target stays held from eng and ci claims until full autonomy or the seat that decides approves the section."""

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.swarm import claim_hold
from scripts.swarm import cli as swarm_cli
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError
from scripts.swarm_ledger import ledger_artifacts
from scripts.swarm_ledger import ledger_core as package_core
from scripts.swarm_ledger.api import schemas
from tests.swarm.test_phase_planning import DONE_WHEN, MASTER_SEAT, SLUG, core, env, items, state  # noqa: F401
from tests.swarm.test_thin_hold import view

MASTER = "master@a1b2c3-0001"
PLANNER = "planner@a1b2c3-0002"
TARGET = "t1"
ENHANCE = "enhance-t1"
ANCHOR = "t1-plan"
Seats = tuple[RedisStore, Any]
HELD = "it waits for its enhancement plan task enhance-t1"
URL = "https://github.com/acme/app/issues/1"
PARTS = (
    "What will be built",
    "Code areas touched",
    "What might break",
    "Regression tests",
    "Resources needed",
    "Protection plan",
    "Summary",
    "Tests",
    "Proof",
)


def section(*missing: str) -> str:
    rows = ["### Seams", "The close calls `enhance_review.close` from swarm done."]
    for name in PARTS:
        if name not in missing:
            rows += [f"### {name}", f"Plain words about {name.lower()}."]
    rows.append("The tests pass in CI." if "done when" in missing else "Done when the tests pass in CI.")
    return "\n".join(rows)


def publish(body: str) -> None:
    text = f"# Plan for p1\n<!-- slice: {ANCHOR} -->\n## Enhance task t1\n{body}\n"
    file: dict[str, Any] = ledger_artifacts.store(SLUG, "plan.md", text.encode())
    name = str(file["id"])
    ref = {"artifact": f"http://127.0.0.1:8765/artifacts/{SLUG}/{name}", "lines": f"1-{len(text.splitlines())}"}
    key = name[:10]
    ops = [
        {"op": "join", "id": f"join-{key}", "by": "planner", "role": "member"},
        {"op": "artifact_add", "id": f"art-{key}", "by": "planner", "task": "", "title": "Plan", "file": file},
        {"op": "phase_update", "id": f"ref-{key}", "by": "planner", "item": "phases/p1"},
    ]
    ops[1]["plan"] = True
    ops[2]["fields"] = {"plan_ref": ref}
    for op in ops:
        core.check_op(op)
    assert core.sync(SLUG, ops=ops)[1] == []


def task(task_id: str) -> dict[str, Any]:
    return next(t for t in state(SLUG)["tasks"] if t["id"] == task_id)


def hold(task_id: str) -> str:
    seen = view(state(SLUG))
    return claim_hold.hold(seen.rows[task_id], seen.rows, seen.doc, seen.graph, "")


def autonomy(store: RedisStore, level: str) -> None:
    store.update(SLUG, autonomy=level)


def cli(capsys: pytest.CaptureFixture[str], name: str, *argv: str) -> tuple[int, Any]:
    code = swarm_cli.main([SLUG, "--as", name, *argv])
    out, err = capsys.readouterr()
    return code, json.loads(out) if code == 0 else err.strip()


def planned(ledger: Any, **plan: Any) -> None:
    ledger.add_task(
        SLUG, {"task": TARGET, "title": "Task t1", "lane": "eng", "phase": "p1", "description": "Thin."}, "swarm"
    )
    fields = {"task": ENHANCE, "title": "Enhance task t1", "lane": "plan", "kind": "plan", "phase": "p1", **plan}
    ledger.add_task(SLUG, {**fields, "description": "Enhance task t1 into a full plan section."}, "swarm")
    ledger.update_task(SLUG, ENHANCE, {"state": "claimed", "claimed_by": PLANNER})


def sliced(ledger: Any, body: str, description: str = DONE_WHEN) -> None:
    publish(body)
    fields = {"plan_url": URL, "plan_slice": ANCHOR, "description": description, "territory": ["scripts/swarm"]}
    ledger.update_task(SLUG, TARGET, fields, by=PLANNER)


@pytest.fixture
def seats(env: Seats, monkeypatch: pytest.MonkeyPatch, memory_path: Path) -> Seats:  # noqa: F811
    store, ledger = env
    monkeypatch.setattr(package_core, "LEDGER_DIR", memory_path)
    store.put_agent(SLUG, AgentRecord(name=MASTER, lane="master", task="master"))
    store.put_agent(SLUG, AgentRecord(name=PLANNER, lane="plan", task=ENHANCE))
    return store, ledger


@pytest.fixture
def thin(seats: Seats) -> Seats:
    store, ledger = seats
    planned(ledger, enhances=TARGET)
    ledger.update_task(SLUG, TARGET, {"enhancement": "needed"})
    assert hold(TARGET) == HELD
    return store, ledger


@pytest.mark.feature("swarm-enhance-close")
def test_an_ordinary_plan_task_still_closes_on_its_slice_proof(
    seats: Seats, capsys: pytest.CaptureFixture[str]
) -> None:
    store, ledger = seats
    planned(ledger)
    sliced(ledger, section())
    code, out = cli(capsys, PLANNER, "done", "--slice", TARGET)
    assert (code, out["state"]) == (0, "done")
    assert (task(ENHANCE)["state"], task(ENHANCE)["proof"]["slice"]) == ("done", TARGET)
    assert "enhancement" not in task(TARGET)
    assert not [i for i in items(store, MASTER_SEAT) if ENHANCE in i.text]


@pytest.mark.feature("swarm-enhance-close")
def test_done_refuses_an_enhancement_whose_target_carries_no_plan_slice(
    thin: Seats, capsys: pytest.CaptureFixture[str]
) -> None:
    autonomy(thin[0], "full")
    code, err = cli(capsys, PLANNER, "done", "--slice", TARGET)
    assert code == 1
    assert "target t1 carries no plan slice" in err
    assert (task(ENHANCE)["state"], task(TARGET)["enhancement"]) == ("claimed", "needed")
    assert hold(TARGET) == HELD


@pytest.mark.feature("swarm-enhance-close")
def test_done_refuses_an_incomplete_section_and_names_every_missing_part(
    thin: Seats, capsys: pytest.CaptureFixture[str]
) -> None:
    store, ledger = thin
    autonomy(store, "full")
    sliced(ledger, section("Protection plan", "done when"))
    code, err = cli(capsys, PLANNER, "done", "--slice", TARGET)
    assert code == 1
    assert "The section has no Protection plan heading. The section has no done when sentence." in err
    assert (task(ENHANCE)["state"], task(TARGET)["enhancement"]) == ("claimed", "needed")
    assert hold(TARGET) == HELD


@pytest.mark.feature("swarm-enhance-close")
def test_full_autonomy_closes_a_complete_enhancement_and_releases_the_target(
    thin: Seats, capsys: pytest.CaptureFixture[str]
) -> None:
    store, ledger = thin
    autonomy(store, "full")
    sliced(ledger, section())
    assert hold(TARGET) == HELD
    code, out = cli(capsys, PLANNER, "done", "--slice", TARGET)
    assert (code, out["state"]) == (0, "done")
    assert (task(ENHANCE)["state"], task(TARGET)["enhancement"]) == ("done", "planned")
    assert hold(TARGET) == ""
    assert not [i for i in items(store, MASTER_SEAT) if ENHANCE in i.text]


@pytest.mark.feature("swarm-enhance-close")
def test_full_autonomy_asks_the_master_when_the_slice_check_faults_the_target(
    thin: Seats, capsys: pytest.CaptureFixture[str]
) -> None:
    store, ledger = thin
    autonomy(store, "full")
    sliced(ledger, section(), description="Too short.")
    code, _ = cli(capsys, PLANNER, "done", "--slice", TARGET)
    assert code == 0
    assert (task(ENHANCE)["state"], task(TARGET)["enhancement"]) == ("done", "needed")
    assert hold(TARGET) == HELD
    [ask] = [i for i in items(store, MASTER_SEAT) if ENHANCE in i.text]
    assert "Task t1 has a description under 20 words." in ask.text
    assert f"plan approve --task {ENHANCE}" in ask.text


@pytest.mark.feature("swarm-enhance-close")
def test_delegate_holds_the_target_until_the_master_approves_the_task(
    thin: Seats, capsys: pytest.CaptureFixture[str]
) -> None:
    store, ledger = thin
    autonomy(store, "delegate")
    sliced(ledger, section())
    assert cli(capsys, PLANNER, "done", "--slice", TARGET)[0] == 0
    assert (task(ENHANCE)["state"], task(TARGET)["enhancement"]) == ("done", "needed")
    assert hold(TARGET) == HELD
    [ask] = [i for i in items(store, MASTER_SEAT) if ENHANCE in i.text]
    assert "The slice check found no problems." in ask.text
    assert f'plan send-back --task {ENHANCE} --note "<what to change>"' in ask.text
    store.put_agent(SLUG, AgentRecord(name="planner@a1b2c3-0009", lane="plan", task="t9"))
    code, err = cli(capsys, "planner@a1b2c3-0009", "plan", "approve", "--task", ENHANCE)
    assert (code, err) == (1, "swarm: only the swarm master reviews an enhancement at delegate autonomy")
    assert hold(TARGET) == HELD
    code, out = cli(capsys, MASTER, "plan", "approve", "--task", ENHANCE)
    assert (code, out) == (0, {"task": ENHANCE, "target": TARGET, "state": "approved"})
    assert task(TARGET)["enhancement"] == "planned"
    assert hold(TARGET) == ""


@pytest.mark.feature("swarm-enhance-close")
@pytest.mark.parametrize("level", ["manual", "assist"])
def test_the_operator_decides_an_enhancement_at_manual_and_assist(
    thin: Seats, capsys: pytest.CaptureFixture[str], level: str
) -> None:
    store, ledger = thin
    autonomy(store, level)
    sliced(ledger, section())
    assert cli(capsys, PLANNER, "done", "--slice", TARGET)[0] == 0
    assert hold(TARGET) == HELD
    [ask] = [i for i in items(store, MASTER_SEAT) if ENHANCE in i.text]
    assert f"the operator decides it at {level} autonomy" in ask.text
    code, err = cli(capsys, MASTER, "plan", "approve", "--task", ENHANCE)
    assert code == 1
    assert err.startswith(f"swarm: at {level} autonomy the operator decides the enhancement of task {TARGET}")
    assert hold(TARGET) == HELD
    ledger.update_task(SLUG, TARGET, {"enhancement": "planned"}, by="operator")
    assert hold(TARGET) == ""


@pytest.mark.feature("swarm-enhance-close")
def test_send_back_reopens_the_enhancement_with_the_note_and_keeps_the_target_held(
    thin: Seats, capsys: pytest.CaptureFixture[str]
) -> None:
    store, ledger = thin
    autonomy(store, "delegate")
    sliced(ledger, section())
    assert cli(capsys, PLANNER, "done", "--slice", TARGET)[0] == 0
    with pytest.raises(SystemExit):
        swarm_cli.main([SLUG, "--as", MASTER, "plan", "send-back", "--task", ENHANCE])
    capsys.readouterr()
    code, out = cli(capsys, MASTER, "plan", "send-back", "--task", ENHANCE, "--note", "Name the parser seam")
    assert (code, out) == (0, {"task": ENHANCE, "target": TARGET, "state": "sent_back"})
    reopened = task(ENHANCE)
    assert (reopened["state"], reopened["claimed_by"]) == ("open", "")
    assert reopened["description"].endswith("Sent back: Name the parser seam")
    assert task(TARGET)["enhancement"] == "needed"
    assert hold(TARGET) == HELD


@pytest.mark.feature("swarm-enhance-close")
def test_a_review_refuses_an_enhancement_that_is_not_in_review(thin: Seats, capsys: pytest.CaptureFixture[str]) -> None:
    store, _ = thin
    autonomy(store, "delegate")
    code, err = cli(capsys, MASTER, "plan", "approve", "--task", ENHANCE)
    assert (code, err) == (1, f"swarm: the enhancement of task {TARGET} is not in review")
    code, err = cli(capsys, MASTER, "plan", "approve", "--task", TARGET)
    assert (code, err) == (1, f"swarm: task {TARGET} enhances no task")
    assert hold(TARGET) == HELD


@pytest.mark.feature("swarm-enhance-close")
@pytest.mark.parametrize("argv", [[], ["p1", "--task", ENHANCE]])
def test_a_review_names_exactly_one_of_a_phase_or_a_task(
    thin: Seats, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    code, err = cli(capsys, MASTER, "plan", "approve", *argv)
    assert (code, err) == (1, "swarm: name exactly one of a phase or --task <enhancement task id>")


@pytest.mark.feature("swarm-enhance-close")
@pytest.mark.parametrize(
    "author", ["planner@a1b2c3-0002", "engineer@a1b2c3-0003", "ci@a1b2c3-0004", "dispatcher@a1b2c3-0005", "swarm"]
)
def test_only_the_master_or_the_operator_may_skip_an_enhancement(thin: Seats, author: str) -> None:
    _, ledger = thin
    with pytest.raises(SwarmError, match="cannot skip the enhancement of task t1"):
        ledger.update_task(SLUG, TARGET, {"enhancement": "skipped"}, by=author)
    assert hold(TARGET) == HELD


@pytest.mark.feature("swarm-enhance-close")
@pytest.mark.parametrize("author", [MASTER, "operator"])
def test_the_master_or_the_operator_skips_an_enhancement(thin: Seats, author: str) -> None:
    _, ledger = thin
    ledger.update_task(SLUG, TARGET, {"enhancement": "skipped"}, by=author)
    assert (task(TARGET)["enhancement"], hold(TARGET)) == ("skipped", "")


@pytest.mark.feature("swarm-enhance-close")
def test_the_operator_sets_only_the_enhancement_of_a_task(thin: Seats) -> None:
    _, ledger = thin
    with pytest.raises(SwarmError, match="an agent name other than operator"):
        ledger.update_task(SLUG, TARGET, {"enhancement": "planned", "state": "done"}, by="operator")
    assert task(TARGET)["state"] == "open"


@pytest.mark.feature("swarm-enhance-close")
def test_enhancement_takes_only_its_four_values(thin: Seats) -> None:
    _, ledger = thin
    with pytest.raises(SwarmError, match="enhancement must be one of"):
        ledger.update_task(SLUG, TARGET, {"enhancement": "maybe"})
    ledger.update_task(SLUG, TARGET, {"enhancement": ""})
    assert task(TARGET)["enhancement"] == ""


@pytest.mark.feature("swarm-enhance-close")
def test_enhances_survives_task_add_and_the_hosted_schema_accepts_it(thin: Seats) -> None:
    assert task(ENHANCE)["enhances"] == TARGET
    assert "enhances" in schemas.FIELDS["task_add"].split()
