"""Consultation receipts bound to the task revision, checked by the brain gate on covered writes (SV2-GAT-04)."""

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from scripts.gates.brain import (
    EXPIRED,
    FAILED_RESULT,
    NO_RECEIPT,
    OTHER_TASK,
    REFUSAL,
    STALE_GENERATION,
    STALE_REVISION,
    UNRELATED,
    BrainGate,
    bind,
)

from hooks.context import brain_adapter, project_cache, task_context
from hooks.context.project_identity import ProjectIdentity
from hooks.context.project_memory import ProjectMemory
from hooks.context.project_render import NO_MEMORY, render_project_block
from hooks.targets import emitter
from scripts.gates import entry, log
from scripts.gates.base import Call, Who
from scripts.swarm_v2.keyspace import stamp
from tests.swarm_ledger import legacy_page

FIXTURE = Path(__file__).parents[1] / "fixtures/swarm_v2/consultation-gates.json"
INPUTS = json.loads(FIXTURE.read_text(encoding="utf-8"))
SWARM, TASK, OTHER = INPUTS["swarm"], *INPUTS["tasks"]
ROWS, PACKS = INPUTS["rows"], INPUTS["packs"]
AGENT = "engineer@100001-0001"


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path / "home")
    monkeypatch.setattr("hooks.config.BRAIN_ENABLED", True)
    monkeypatch.setattr("hooks.config.BRAIN_REFRESH_TOOL_CALLS", 1)
    for name in ("AGENTIHOOKS_TASK_CONTEXT_OFF", "AGENTIHOOKS_BRAIN_RECEIPT_TTL"):
        monkeypatch.delenv(name, raising=False)
    ledgers = tmp_path / "ledgers"
    ledgers.mkdir()
    env = {"AGENTIHOOKS_TARGET": "claude", "LEDGER_DIR": str(ledgers), "AGENTIHOOKS_SWARM": SWARM}
    for name, value in {**env, "AGENTIHOOKS_SWARM_TASK": TASK}.items():
        monkeypatch.setenv(name, value)
    emitter.drain()

    def seed(**rows):
        doc = {"tasks": [{"id": task, **ROWS[row]} for task, row in rows.items()]}
        legacy_page.store(ledgers, SWARM, doc)

    yield {"seed": seed, "tmp": tmp_path, "env": env}
    emitter.drain()


def deliver(session, pack):
    with (
        patch("hooks.context.brain_adapter._refresh", return_value=None),
        patch("hooks.context.project_cache.project_context", return_value=pack),
    ):
        return brain_adapter.maybe_refresh_on_tool_call(session, 1)


def write(session, task=TASK):
    call = Call.from_payload({**INPUTS["calls"]["covered"][0], "session_id": session})
    return BrainGate().decide(call, Who(name=AGENT, swarm=SWARM, lane="eng", task=task), None)


def refused(reason, task=TASK):
    return REFUSAL.format(reason=reason, task=task)


def bound(session):
    return {
        k: v
        for k, v in task_context.consultation(session).items()
        if k in ("purpose", "task", "revision", "generation", "brain")
    }


def brain():
    return project_cache.namespace().brain


@pytest.mark.parametrize("task", INPUTS["tasks"])
def test_a_first_write_needs_a_receipt_and_repeated_calls_search_nothing_more(world, monkeypatch, task):
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", task)
    world["seed"](**{task: "first"})
    session = f"a-{task}"
    assert write(session, task).reason == refused(NO_RECEIPT, task)

    assert deliver(session, PACKS["first"]) == PACKS["first"]
    receipt = task_context.consultation(session)
    assert bound(session) == bind(task, ROWS["first"], brain())
    assert receipt["result"] == "delivered"
    assert receipt["digest"] == task_context.digest(PACKS["first"])
    assert receipt["expires"] - receipt["at"] == 14400

    blocks = 0
    for payload in INPUTS["calls"]["ordinary"]:
        assert not BrainGate().matches(Call.from_payload(payload))
        assert deliver(session, PACKS["first"]) is None
        blocks += not write(session, task).allowed
    assert blocks == 0
    assert task_context.outcomes()["claude"] == {"delivered": 1, "deduplicated": 4}


def test_a_valid_empty_result_counts_as_consultation(world):
    world["seed"](**{TASK: "first"})
    assert render_project_block(ProjectMemory("alpha"), 4096) == PACKS["empty"]
    assert NO_MEMORY == "No project memory yet."
    deliver("empty", PACKS["empty"])
    assert task_context.consultation("empty")["result"] == "empty"
    assert write("empty").allowed


def test_b_an_unrelated_query_receipt_cannot_satisfy_the_gate(world):
    world["seed"](**{TASK: "first"})
    unrelated = stamp(project_cache.namespace(), "consultation", {**INPUTS["unrelated"], "expires": time.time() + 60})
    project_cache._write(task_context._consultation_path("b"), unrelated)
    before = task_context.consultation("b")
    assert write("b").reason == refused(UNRELATED)
    assert task_context.consultation("b") == before == unrelated


def test_b_another_tasks_receipt_cannot_satisfy_the_gate(world, monkeypatch):
    world["seed"](**{TASK: "first", OTHER: "first"})
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", OTHER)
    deliver("b2", PACKS["first"])
    assert bound("b2") == bind(OTHER, ROWS["first"], brain())
    assert write("b2", OTHER).allowed
    assert write("b2", TASK).reason == refused(OTHER_TASK)


def test_b_a_brain_failure_is_not_consultation_and_is_retried(world):
    world["seed"](**{TASK: "first"})
    identity = ProjectIdentity(project="alpha", repo="alpha")
    cached = {"memory": {"project": "alpha"}, "outage_at": 1.0, "at": time.time()}
    assert project_cache.UNREACHABLE in project_cache._cached_memory(cached, identity)[1]
    assert project_cache.UNREACHABLE in PACKS["failed"]
    assert deliver("fail", PACKS["failed"]) == PACKS["failed"]
    assert task_context.consultation("fail")["result"] == "failed"
    assert write("fail").reason == refused(FAILED_RESULT)
    assert deliver("fail", PACKS["failed"]) == PACKS["failed"]
    assert task_context.outcomes()["claude"] == {"delivered": 2}


def test_b_rollback_to_observe_keeps_the_receipt_and_the_gate_log(world):
    world["seed"](**{TASK: "first"})
    env = {**world["env"], "AGENTIHOOKS_SWARM_TASK": TASK, "AGENTIHOOKS_AGENT_NAME": AGENT}
    home = world["tmp"] / "gates"
    call = Call.from_payload({**INPUTS["calls"]["covered"][0], "session_id": "rb"})
    project_cache._write(
        task_context._consultation_path("rb"), stamp(project_cache.namespace(), "consultation", INPUTS["unrelated"])
    )
    gate = entry.GATES["brain"]
    with patch("scripts.gates.modes.swarm_gates", return_value={"brain": "enforce"}):
        assert entry.evaluate(gate, call, env, home) == 2
    with patch("scripts.gates.modes.swarm_gates", return_value={"brain": "observe"}):
        assert entry.evaluate(gate, call, env, home) == 0
    rows = [(row["gate"], row["kind"], row["reason"]) for row in log.recent(SWARM, home=home)]
    assert rows == [("brain", "deny", refused(UNRELATED)), ("brain", "observe", refused(UNRELATED))]
    assert task_context.consultation("rb")["purpose"] == "search"


def test_c_a_spec_change_invalidates_the_receipt_until_a_new_pack_lands(world):
    world["seed"](**{TASK: "first"})
    deliver("c", PACKS["first"])
    assert write("c").allowed
    world["seed"](**{TASK: "changed"})
    assert write("c").reason == refused(STALE_REVISION)
    assert deliver("c", PACKS["first"]) == PACKS["first"]
    assert bound("c") == bind(TASK, ROWS["changed"], brain())
    assert write("c").allowed


def test_c_a_new_claim_invalidates_the_receipt_until_a_new_pack_lands(world):
    world["seed"](**{TASK: "first"})
    deliver("g", PACKS["first"])
    world["seed"](**{TASK: "reclaimed"})
    assert write("g").reason == refused(STALE_GENERATION)
    assert deliver("g", PACKS["first"]) == PACKS["first"]
    assert write("g").allowed


def test_c_a_state_move_keeps_the_receipt(world):
    world["seed"](**{TASK: "first"})
    deliver("m", PACKS["first"])
    world["seed"](**{TASK: "moved"})
    assert write("m").allowed
    assert deliver("m", PACKS["first"]) is None


def test_c_an_expired_receipt_is_refused_and_redelivered(world, monkeypatch):
    world["seed"](**{TASK: "first"})
    monkeypatch.setenv("AGENTIHOOKS_BRAIN_RECEIPT_TTL", "-1")
    deliver("x", PACKS["first"])
    assert write("x").reason == refused(EXPIRED)
    assert deliver("x", PACKS["first"]) == PACKS["first"]
    monkeypatch.setenv("AGENTIHOOKS_BRAIN_RECEIPT_TTL", "60")
    assert deliver("x", PACKS["first"]) == PACKS["first"]
    receipt = task_context.consultation("x")
    assert receipt["expires"] - receipt["at"] == 60
    assert write("x").allowed


def test_outside_a_swarm_task_no_consultation_is_written(world, monkeypatch):
    world["seed"](**{TASK: "first"})
    monkeypatch.delenv("AGENTIHOOKS_SWARM_TASK")
    assert deliver("o", PACKS["first"]) == PACKS["first"]
    assert task_context.consultation("o") == {}
    assert deliver("o", PACKS["first"]) is None
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", TASK)
    monkeypatch.delenv("AGENTIHOOKS_SWARM")
    assert deliver("o2", PACKS["first"]) == PACKS["first"]
    assert task_context.consultation("o2") == {}
