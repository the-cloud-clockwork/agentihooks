"""Enhancement close: a plan task carrying enhances closes only on a complete plan section for its target, and the
target stays held from eng and ci claims until full autonomy or the seat that decides approves the section."""

import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from scripts.swarm import claim_hold, enhance_review, enhance_sweep, plan_review, prompt, slice_check
from scripts.swarm import cli as swarm_cli
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError
from scripts.swarm_ledger import ledger as ledger_cli
from scripts.swarm_ledger import ledger_artifacts, ledger_enhancement, ledger_link, plan_read
from scripts.swarm_ledger import ledger_core as package_core
from scripts.swarm_ledger.api import schemas
from scripts.tick import registry
from scripts.tick.adapters.fakes import FakeLedger, fake_tools
from scripts.tick.deadline import Deadline
from scripts.tick.steps import CallStep
from scripts.tick.steps import thin as thin_step
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
FAULTS = "Task t1 has a description under 20 words. Task t1 names no territory."
MISSING = "The section has no Protection plan heading. The section has no done when sentence."
VALID = {"op": "task_enhancement", "id": "e1", "by": "swarm", "item": "tasks/t1", "enhancement": "needed"}
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
    rows = ["### Seams", "The tests observe swarm done exits zero after `enhance_review.close`."]
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


def enhance(by: str, value: str, task_id: str = TARGET) -> tuple[dict[str, Any], list[str]]:
    op: dict[str, Any] = {**VALID, "id": f"enhance-{uuid.uuid4().hex[:10]}", "by": by, "item": f"tasks/{task_id}"}
    op["enhancement"] = value
    core.check_op(op)
    found, rejected = core.sync(SLUG, ops=[op])
    return found, rejected


def refused(by: str, value: str, task_id: str = TARGET) -> str:
    found, rejected = enhance(by, value, task_id)
    assert len(rejected) == 1
    return " ".join(found["_meta"]["warnings"])


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


def asked(store: RedisStore) -> str:
    [ask] = [i for i in items(store, MASTER_SEAT) if ENHANCE in i.text]
    return str(ask.text)


@dataclass
class Applying(FakeLedger):
    """A tick ledger port that applies every batch to the test ledger through the ledger server's own checks."""

    def mutate(self, operations: Sequence[Mapping[str, object]], *, deadline: Deadline) -> object:
        ops = [dict(operation) for operation in operations]
        core.check_body({"ops": ops})
        found, rejected = core.sync(SLUG, ops=ops)
        assert rejected == []
        return found


def thin_pass() -> list[str]:
    """Run the thin task pass tick step once over the test ledger, writing through Applying."""
    step = {found.name: found for found in registry.steps()}["thin task pass"]
    tools = replace(fake_tools(), ledger=Applying()).for_effects(frozenset({"inbox", "ledger"}))
    values: dict[str, Any] = {
        "doc": state(SLUG),
        "tools": tools,
        "deadline": Deadline.after(5),
        "slug": SLUG,
        "inbox": Mock(),
    }
    assert isinstance(step, CallStep)
    lines: list[str] = step.call(None, values.__getitem__)
    return lines


def served(slug: str, ops: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    found, rejected = core.sync(slug, ops=ops)
    return {**found, "rejected": rejected}


def publish_plan(monkeypatch: pytest.MonkeyPatch, path: Path, phase: str) -> None:
    """Run the planner's publish-plan command into ``phase`` against the test ledger, storing the plan as a ledger
    artifact."""
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", ENHANCE)
    monkeypatch.setattr(ledger_cli, "call", served)
    monkeypatch.setattr(
        ledger_cli,
        "upload_artifact",
        lambda slug, name, source, request: ledger_artifacts.store(slug, "plan.md", Path(source).read_bytes()),
    )
    monkeypatch.setattr(
        ledger_cli.ledger_publish,
        "publish",
        lambda source, title, repo, artifact, issue_title: (artifact(source, title), "artifact"),
    )
    argv = ["--slug", SLUG, "--as", PLANNER, "publish-plan", str(path), "--phase", phase]
    ledger_cli.cmd_publish_plan(ledger_cli.build_parser().parse_args(argv))


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
    ledger.set_enhancement(SLUG, TARGET, "needed", "swarm")
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
    assert enhance_review.UNSLICED.format(task=ENHANCE, target=TARGET) in err
    assert (task(ENHANCE)["state"], task(TARGET)["enhancement"]) == ("claimed", "needed")
    assert hold(TARGET) == HELD


@pytest.mark.feature("swarm-enhance-close")
def test_done_refuses_an_enhancement_of_a_task_the_ledger_does_not_hold(
    seats: Seats, capsys: pytest.CaptureFixture[str]
) -> None:
    store, ledger = seats
    autonomy(store, "full")
    planned(ledger, enhances="t9")
    sliced(ledger, section())
    code, err = cli(capsys, PLANNER, "done", "--slice", TARGET)
    assert code == 1
    assert enhance_review.UNSLICED.format(task=ENHANCE, target="t9") in err
    assert task(ENHANCE)["state"] == "claimed"


@pytest.mark.feature("swarm-enhance-close")
def test_done_refuses_an_incomplete_section_and_names_every_missing_part(
    thin: Seats, capsys: pytest.CaptureFixture[str]
) -> None:
    store, ledger = thin
    autonomy(store, "full")
    sliced(ledger, section("Protection plan", "done when"))
    code, err = cli(capsys, PLANNER, "done", "--slice", TARGET)
    assert code == 1
    assert enhance_review.INCOMPLETE.format(target=TARGET, problems=MISSING) in err
    assert (task(ENHANCE)["state"], task(TARGET)["enhancement"]) == ("claimed", "needed")
    assert hold(TARGET) == HELD


@pytest.mark.feature("swarm-enhance-close")
def test_full_autonomy_closes_a_complete_enhancement_and_releases_the_target(
    thin: Seats, capsys: pytest.CaptureFixture[str]
) -> None:
    store, ledger = thin
    autonomy(store, "full")
    sliced(ledger, section())
    ledger.update_task(SLUG, TARGET, {"depends_on": [ENHANCE]}, by=PLANNER)
    code, out = cli(capsys, PLANNER, "done", "--slice", TARGET)
    assert (code, out["state"]) == (0, "done")
    assert (task(ENHANCE)["state"], task(TARGET)["enhancement"]) == ("done", "planned")
    assert hold(TARGET) == ""
    assert not [i for i in items(store, MASTER_SEAT) if ENHANCE in i.text]


@pytest.mark.feature("thin-phaseless-enhance-publish")
def test_the_planner_of_a_thin_task_with_no_phase_publishes_into_its_own_phase_and_closes(
    seats: Seats, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A thin task added with no phase gets from the thin task pass a manual phase of its own holding it and its
    enhancement task; the claimed planner publishes its plan into the phase its prompt names with publish-plan, points
    the task at its section and closes its plan task with done, which marks the task planned and releases its hold."""
    store, ledger = seats
    autonomy(store, "full")
    ledger.add_task(SLUG, {"task": TARGET, "title": "Task t1", "lane": "eng", "description": "Thin."}, "swarm")
    assert thin_step.unchecked(task(TARGET))
    assert thin_pass() == ["queued enhance-t1 for thin task t1"]
    assert (task(TARGET)["phase"], task(ENHANCE)["phase"], hold(TARGET)) == (ENHANCE, ENHANCE, HELD)
    ledger.update_task(SLUG, ENHANCE, {"state": "claimed", "claimed_by": PLANNER})
    assert core.sync(SLUG, ops=[{"op": "join", "id": "join-enhancer", "by": PLANNER, "role": "member"}])[1] == []
    plan = tmp_path / "plan.md"
    plan.write_text(
        f"# Plan for task t1\n<!-- slice: {ANCHOR} -->\n## Enhance task t1\n{section()}\n", encoding="utf-8"
    )
    told = re.search(
        r"publish-plan <plan file> --phase (\S+)\.", prompt.build(SLUG, "/repo", "plan", PLANNER, task(ENHANCE))
    )
    assert told is not None and told.group(1) == ENHANCE
    publish_plan(monkeypatch, plan, told.group(1))
    url = json.loads(capsys.readouterr().out)["plan_url"]
    fields = {"plan_url": url, "plan_slice": ANCHOR, "description": DONE_WHEN, "territory": ["scripts/swarm"]}
    ledger.update_task(SLUG, TARGET, fields, by=PLANNER)
    code, out = cli(capsys, PLANNER, "done", "--slice", TARGET)
    assert (code, out["state"]) == (0, "done")
    assert (task(ENHANCE)["state"], task(TARGET)["enhancement"], hold(TARGET)) == ("done", "planned", "")


@pytest.mark.feature("swarm-enhance-close")
def test_the_close_reads_the_target_section_through_the_hosted_loader(
    thin: Seats, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, ledger = thin
    autonomy(store, "full")
    sliced(ledger, section())
    loads: list[str] = []
    fetched = plan_read.fetched

    def load(name: str, file_id: str) -> bytes:
        loads.append(name)
        data: bytes = ledger_artifacts.read(name, file_id)
        return data

    def hosted(slug: str, environ: Mapping[str, str]) -> tuple[dict[str, Any] | None, Callable[[str, str], bytes]]:
        return fetched(slug, environ)[0], load

    monkeypatch.setattr(plan_read, "fetched", hosted)
    assert cli(capsys, PLANNER, "done", "--slice", TARGET)[0] == 0
    assert (loads, task(TARGET)["enhancement"]) == ([SLUG], "planned")


@pytest.mark.feature("swarm-enhance-close")
def test_full_autonomy_asks_the_master_when_the_slice_check_faults_the_target(
    thin: Seats, capsys: pytest.CaptureFixture[str]
) -> None:
    store, ledger = thin
    autonomy(store, "full")
    sliced(ledger, section(), description="Too short.")
    ledger.update_task(SLUG, TARGET, {"territory": []}, by=PLANNER)
    code, _ = cli(capsys, PLANNER, "done", "--slice", TARGET)
    assert code == 0
    assert (task(ENHANCE)["state"], task(TARGET)["enhancement"]) == ("done", "needed")
    assert hold(TARGET) == HELD
    assert asked(store) == enhance_review.REVIEW.format(task=ENHANCE, target=TARGET, slug=SLUG, found=FAULTS)
    code, out = cli(capsys, MASTER, "plan", "approve", "--task", ENHANCE)
    assert (code, out) == (0, {"task": ENHANCE, "target": TARGET, "state": "approved"})
    assert (task(TARGET)["enhancement"], hold(TARGET)) == ("planned", "")


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
    found = enhance_review.CLEAN
    assert asked(store) == enhance_review.REVIEW.format(task=ENHANCE, target=TARGET, slug=SLUG, found=found)
    store.put_agent(SLUG, AgentRecord(name="planner@a1b2c3-0009", lane="plan", task="t9"))
    code, err = cli(capsys, "planner@a1b2c3-0009", "plan", "approve", "--task", ENHANCE)
    assert (code, err) == (1, f"swarm: {enhance_review.MASTER_ONLY.format(autonomy='delegate')}")
    assert hold(TARGET) == HELD
    code, out = cli(capsys, MASTER, "plan", "approve", "--task", ENHANCE)
    assert (code, out) == (0, {"task": ENHANCE, "target": TARGET, "state": "approved"})
    assert (task(TARGET)["enhancement"], hold(TARGET)) == ("planned", "")


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
    relay = enhance_review.RELAY.format(task=ENHANCE, target=TARGET, autonomy=level, found=enhance_review.CLEAN)
    assert asked(store) == relay
    code, err = cli(capsys, MASTER, "plan", "approve", "--task", ENHANCE)
    assert (code, err) == (1, f"swarm: {enhance_review.OPERATOR.format(autonomy=level, target=TARGET)}")
    assert hold(TARGET) == HELD
    ledger.set_enhancement(SLUG, TARGET, "planned", "operator")
    assert (task(TARGET)["enhancement"], hold(TARGET)) == ("planned", "")


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
    described = "Enhance task t1 into a full plan section.\n\nSent back: Name the parser seam"
    assert (reopened["state"], reopened["claimed_by"], reopened["description"]) == ("open", "", described)
    assert state(SLUG)["_meta"]["stamps"][f"tasks/{ENHANCE}/state"]["by"] == MASTER
    assert (task(TARGET)["enhancement"], hold(TARGET)) == ("needed", HELD)


@pytest.mark.feature("swarm-enhance-close")
def test_a_review_refuses_an_enhancement_that_is_not_in_review(thin: Seats, capsys: pytest.CaptureFixture[str]) -> None:
    store, _ = thin
    autonomy(store, "delegate")
    code, err = cli(capsys, MASTER, "plan", "approve", "--task", ENHANCE)
    assert (code, err) == (1, f"swarm: {enhance_review.NOT_IN_REVIEW.format(target=TARGET)}")
    code, err = cli(capsys, MASTER, "plan", "approve", "--task", TARGET)
    assert (code, err) == (1, f"swarm: {enhance_review.UNTARGETED.format(task=TARGET)}")
    assert hold(TARGET) == HELD


@pytest.mark.feature("swarm-enhance-close")
@pytest.mark.parametrize("argv", [[], ["p1", "--task", ENHANCE]])
def test_a_review_names_exactly_one_of_a_phase_or_a_task(
    thin: Seats, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    code, err = cli(capsys, MASTER, "plan", "approve", *argv)
    assert (code, err) == (1, f"swarm: {plan_review.NAME_ONE}")


@pytest.mark.feature("swarm-enhance-close")
@pytest.mark.parametrize(
    "change", [{"extra": 1}, {"item": "phases/p1"}, {"by": 1}, {"by": ""}, {"enhancement": "maybe"}]
)
def test_the_enhancement_op_refuses_a_malformed_write(change: dict[str, Any]) -> None:
    ledger_enhancement.check(VALID)
    with pytest.raises(ValueError, match=f"^{re.escape(ledger_enhancement.SHAPE)}$"):
        ledger_enhancement.check({**VALID, **change})


@pytest.mark.feature("swarm-enhance-close")
@pytest.mark.parametrize(
    "author", ["planner@a1b2c3-0002", "engineer@a1b2c3-0003", "ci@a1b2c3-0004", "dispatcher@a1b2c3-0005", "swarm"]
)
def test_only_the_master_or_the_operator_may_skip_an_enhancement(thin: Seats, author: str) -> None:
    assert refused(author, "skipped") == ledger_enhancement.SKIP.format(by=author, task=TARGET)
    assert hold(TARGET) == HELD


@pytest.mark.feature("swarm-enhance-close")
@pytest.mark.parametrize(("author", "lane"), [("engineer@a1b2c3-0003", "eng"), ("ci@a1b2c3-0004", "ci")])
def test_an_eng_or_ci_worker_never_sets_an_enhancement(thin: Seats, author: str, lane: str) -> None:
    assert refused(author, "planned") == ledger_enhancement.WORKER.format(by=author, lane=lane, task=TARGET)
    assert hold(TARGET) == HELD


@pytest.mark.feature("swarm-enhance-close")
@pytest.mark.parametrize("author", [PLANNER, "dispatcher@a1b2c3-0005", "swarm"])
def test_only_a_closed_enhancement_the_master_or_the_operator_marks_a_task_planned(thin: Seats, author: str) -> None:
    assert refused(author, "planned") == ledger_enhancement.PLANNED.format(by=author, task=TARGET)
    assert hold(TARGET) == HELD


@pytest.mark.feature("swarm-enhance-close")
@pytest.mark.parametrize("author", [MASTER, "operator"])
def test_the_master_or_the_operator_skips_an_enhancement(thin: Seats, author: str) -> None:
    found, rejected = enhance(author, "skipped")
    [event] = [e for e in found["_meta"]["events"] if e["kind"] == "enhancement set" and e["text"] == "skipped"]
    assert rejected == []
    assert (event["by"], event["target"]) == (author, f"tasks/{TARGET}")
    assert found["_meta"]["stamps"][f"tasks/{TARGET}/enhancement"]["by"] == author
    assert (task(TARGET)["enhancement"], hold(TARGET)) == ("skipped", "")


@pytest.mark.feature("swarm-enhance-close")
def test_the_enhancement_op_refuses_a_task_the_ledger_does_not_hold(thin: Seats) -> None:
    assert refused("swarm", "needed", "t9") == ledger_enhancement.UNKNOWN.format(task="t9")


@pytest.mark.feature("swarm-enhance-close")
def test_a_task_update_never_writes_an_enhancement(thin: Seats) -> None:
    with pytest.raises(SwarmError, match="task_update may set only"):
        thin[1].update_task(SLUG, TARGET, {"enhancement": "planned"})
    assert hold(TARGET) == HELD


@pytest.mark.feature("swarm-enhance-close")
def test_enhances_survives_task_add_and_the_hosted_schemas_accept_the_fields(thin: Seats) -> None:
    assert task(ENHANCE)["enhances"] == TARGET
    assert "enhances" in schemas.FIELDS["task_add"].split()
    assert schemas.FIELDS["task_enhancement"].split() == ["by", "item", "enhancement", "expected"]


@pytest.mark.feature("swarm-enhance-close")
def test_the_task_slice_check_grades_a_target_in_its_own_phase() -> None:
    target = {"id": "t5", "phase": "p1", "description": DONE_WHEN, "territory": ["a", "b"], "depends_on": ["t6", "t7"]}
    tasks = [target, {"id": "t6", "phase": "p1"}, {"id": "t7", "phase": "p2"}]
    doc = {"phases": [{"id": "p1"}, {"id": "p2"}], "tasks": tasks}
    assert slice_check.task_problems(target, doc, slice_check.Limits(max_areas=1)) == [
        "Task t5 names 2 areas, more than 1.",
        "Task t5 depends on t7 in a phase that is not done.",
    ]


@pytest.mark.feature("swarm-enhance-close")
def test_the_plan_reader_fetches_a_hosted_ledger_with_its_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    environ = {"AGENTIHOOKS_SWARM": SLUG}

    def remote(found: Mapping[str, str]) -> bool:
        return found is environ

    def hosted(slug: str, keys: tuple[str, ...]) -> dict[str, Any]:
        return {"slug": slug, "keys": keys}

    def served(found: Mapping[str, str], slug: str, file_id: str) -> bytes:
        return f"{found is environ} {slug} {file_id}".encode()

    monkeypatch.setattr(ledger_link, "remote", remote)
    monkeypatch.setattr(plan_read, "_hosted", hosted)
    monkeypatch.setattr(plan_read, "_served", served)
    doc, load = plan_read.fetched(SLUG, environ)
    assert doc == {"slug": SLUG, "keys": ("tasks", "phases", "artifacts")}
    assert load is not None and load(SLUG, "plan.md") == f"True {SLUG} plan.md".encode()


@pytest.mark.feature("task-enhancement-expected-value")
@pytest.mark.parametrize(
    "held,expected",
    [
        ({}, "needed"),
        ({"enhancement": None}, ""),
        ({"enhancement": ""}, None),
        ({"enhancement": "needed"}, ""),
        ({"enhancement": "planned"}, None),
        ({"enhancement": "skipped"}, ""),
    ],
)
def test_a_stale_enhancement_expectation_refuses_without_effects(held: dict[str, Any], expected: str | None) -> None:
    row = {"id": TARGET, **held}
    doc = {"tasks": [row]}
    ctx = Mock()
    ctx.refused = []
    operation = {**VALID, "expected": expected}
    assert ledger_enhancement.apply(doc, operation, ctx) is False
    assert ctx.refused == [f"task {TARGET} enhancement changed"]
    assert row == {"id": TARGET, **held}
    ctx.stamp.assert_not_called()
    ctx.record.assert_not_called()


@pytest.mark.feature("task-enhancement-expected-value")
@pytest.mark.parametrize(
    "held",
    [
        {},
        {"enhancement": None},
        {"enhancement": ""},
        {"enhancement": "needed"},
        {"enhancement": "planned"},
        {"enhancement": "skipped"},
    ],
)
def test_a_matching_enhancement_expectation_passes_the_api_and_writes(held: dict[str, Any]) -> None:
    row = {"id": TARGET, **held}
    operation = {**VALID, "expected": held.get("enhancement")}
    schemas.check_operations({"operation_id": "guarded", "ops": [operation], "guards": {}}, core, (TARGET,))
    ctx = Mock()
    ctx.refused = []
    assert ledger_enhancement.apply({"tasks": [row]}, operation, ctx) is True
    assert row == {"id": TARGET, "enhancement": "needed"}
    assert ctx.refused == []
    ctx.stamp.assert_called_once_with(f"tasks/{TARGET}/enhancement", "swarm")
    ctx.record.assert_called_once_with("swarm", "enhancement set", f"tasks/{TARGET}", text="needed")


@pytest.mark.feature("task-enhancement-expected-value")
def test_an_omitted_enhancement_expectation_keeps_legacy_writes() -> None:
    row = {"id": TARGET, "enhancement": "planned"}
    ctx = Mock()
    ctx.refused = []
    ledger_enhancement.check(VALID)
    assert ledger_enhancement.apply({"tasks": [row]}, VALID, ctx) is True
    assert row["enhancement"] == "needed"
    assert ctx.refused == []


@pytest.mark.feature("task-enhancement-expected-value")
@pytest.mark.parametrize("expected", [1, True, [], {}, "other"])
def test_the_enhancement_expectation_rejects_invalid_values(expected: Any) -> None:
    with pytest.raises(ValueError) as error:
        ledger_enhancement.check({**VALID, "expected": expected})
    assert str(error.value) == ledger_enhancement.SHAPE


STATED = (
    "Release a held task whose spec already states its proof, so the enhancement sweep marks it skipped and scopes "
    "out its open enhancement plan task, and no engineer claim waits on a planner for work the master already "
    "specified in full. Done when the release lands through the ledger."
)


@pytest.mark.feature("swarm-enhance-release")
def test_the_master_release_frees_a_held_task_whose_spec_states_its_proof_through_the_ledger(seats: Seats) -> None:
    _, ledger = seats
    target = {"task": TARGET, "title": "Task t1", "lane": "eng", "phase": "p1", "description": STATED}
    ledger.add_task(SLUG, target, "swarm")
    plan = {"task": ENHANCE, "title": "Enhance task t1", "lane": "plan", "kind": "plan", "phase": "p1"}
    ledger.add_task(
        SLUG, {**plan, "enhances": TARGET, "description": "Enhance task t1 into a full plan section."}, "swarm"
    )
    ledger.set_enhancement(SLUG, TARGET, "needed", "swarm")
    assert hold(TARGET) == HELD
    released: list[list[dict[str, Any]]] = []

    class Checked(Applying):
        def mutate(self, operations: Sequence[Mapping[str, object]], *, deadline: Deadline) -> object:
            ops = [dict(operation) for operation in operations]
            schemas.check_operations({"operation_id": "release", "ops": ops, "guards": {}}, core, (TARGET, ENHANCE))
            released.append(ops)
            return super().mutate(operations, deadline=deadline)

    result = getattr(enhance_sweep, "release")(state(SLUG)["tasks"], Checked(), MASTER, pause=lambda _: None)
    assert (result.lines, result.released, result.failed) == (
        ("released thin task t1: its spec states its proof",),
        1,
        0,
    )
    assert [op["op"] for op in released[0]] == ["task_enhancement", "set"]
    assert (task(TARGET)["enhancement"], task(ENHANCE)["out_of_scope"]) == ("skipped", True)
    assert hold(TARGET) == ""
