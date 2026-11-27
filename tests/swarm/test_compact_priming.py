import copy
import shlex
from typing import Any

import pytest

import scripts.seat_brief as seat_brief
from scripts.handoff import transfers
from scripts.seat_brief.sources import Brief
from scripts.swarm import priming_trace, prompt

SENTINELS = ("handoff-body-sentinel", "culture-sentinel", "latest-sentinel", "older-sentinel", "learned-sentinel")
ACTION = "Read agentihooks msg inbox and check the operator's order."
LANES = ("eng", "ci", "plan", "review", "master", "dispatch", "expert")


def _task(lane: str, harness: str, history: str = "full", size: int = 1) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": "t1",
        "title": "Finish the continuity mission",
        "description": "Preserve the task instructions.",
        "kind": "review" if lane == "review" else "plan" if lane == "plan" else "code",
        "generation": 2,
        "mission": "mission.md",
        "seat": f"{lane}-1@sw",
        "harness": harness,
    }
    if history == "cold":
        return task
    handoff = f"# Handoff v2\n## Done\n{SENTINELS[0] * size}\n## Next\n1. {ACTION}\n## Read first\nNone\n"
    task["handoff_envelope"] = {
        "reason": "compact",
        "conversation_id": "previous-conversation",
        "ledger": "sw",
        "orders": ["Finish the outstanding order."],
    }
    if history != "legacy":
        task["transfer"] = {"id": "transfer-fixed", "next": ACTION, "handoff": handoff}
    if history == "transfer-only":
        return task
    task.update(
        handoff=handoff,
        culture=SENTINELS[1] * size,
        recaps=[{"occupant": "earlier", "task": "t0", "text": text * size} for text in SENTINELS[2:4]],
        learned=[{"maturity": "note", "text": SENTINELS[4] * size}],
    )
    return task


@pytest.fixture(autouse=True)
def isolated_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prompt, "summary_lines", lambda slug: [])


@pytest.mark.feature("seat-brief")
@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("history", ["cold", "full", "transfer-only", "legacy"])
def test_every_seated_builder_defers_continuity_to_one_brief(lane: str, harness: str, history: str) -> None:
    task = _task(lane, harness, history)
    before = copy.deepcopy(task)
    text = prompt.build("sw", "/repo", lane, "successor", task)
    assert text.count(prompt.SEAT_BRIEF) == 1
    assert all(sentinel not in text for sentinel in SENTINELS)
    assert task == before
    if history != "cold":
        assert text.count("Finish the outstanding order.") == 1
        assert '"conversation_id": "previous-conversation"' in text
    if history in ("full", "transfer-only"):
        assert text.count(f"agentihooks swarm sw confirm-handoff transfer-fixed --next {shlex.quote(ACTION)}") == 1
    if lane in ("eng", "ci", "plan", "review"):
        assert "Preserve the task instructions." in text
    if lane == "expert":
        assert "Finish the continuity mission" in text and "mission.md" in text


@pytest.mark.feature("seat-brief")
@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("lane", LANES)
def test_growing_continuity_does_not_grow_any_opening(lane: str, harness: str) -> None:
    small = prompt.build("sw", "/repo", lane, "successor", _task(lane, harness))
    large = prompt.build("sw", "/repo", lane, "successor", _task(lane, harness, size=128))
    assert large == small


@pytest.mark.feature("seat-brief")
@pytest.mark.parametrize("lane", LANES)
def test_carried_saved_openings_stay_verbatim(lane: str) -> None:
    saved = "Saved opening\n" + "\n".join(SENTINELS)
    assert prompt.build("sw", "/repo", lane, "successor", {prompt.CARRIED: saved}) == saved


@pytest.mark.feature("seat-brief")
def test_compact_transfer_keeps_exact_confirmation_without_the_document() -> None:
    transfer = _task("eng", "claude")["transfer"]
    text = transfers.priming("sw", transfer)
    assert f"agentihooks swarm sw confirm-handoff transfer-fixed --next {shlex.quote(ACTION)}" in text
    assert SENTINELS[0] not in text
    assert transfers.priming("sw", None) == ""
    assert transfers.priming("sw", {"handoff": ""}) == ""


@pytest.mark.feature("seat-brief")
def test_a_seatless_transfer_without_a_handoff_reads_as_no_history() -> None:
    assert prompt.priming_lines({"id": "p", "transfer": {"avoid_harness": "codex"}}) == [
        "Your seat has no history yet: no handoff document, no recap and no learned notes.",
        prompt.SEAT_BRIEF,
    ]


@pytest.mark.feature("seat-brief")
def test_deferred_bodies_are_not_traced_as_delivered_inline() -> None:
    task = _task("eng", "claude")
    assert priming_trace.rows("sw", task) == []


@pytest.mark.feature("seat-brief")
def test_the_same_continuity_still_renders_as_the_complete_four_part_brief() -> None:
    task = _task("eng", "claude")
    brief = Brief(task["seat"], task["handoff"], task["culture"], task["recaps"][0]["text"], "- note: " + SENTINELS[4])
    assert seat_brief.render(brief) == (
        f"Seat eng-1@sw.\nHandoff:\n{task['handoff'].strip()}\nCulture:\n{SENTINELS[1]}\n"
        f"Latest recap:\n{SENTINELS[2]}\nLearned notes:\n- note: {SENTINELS[4]}"
    )
    assert SENTINELS[3] not in seat_brief.render(brief)
