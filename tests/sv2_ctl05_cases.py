import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.swarm.store import SwarmError
from scripts.swarm_ledger.api.resources import revision
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
from scripts.swarm_v2.authority import TaskAuthority
from scripts.swarm_v2.controller import Controller
from scripts.swarm_v2.outcomes import Outcomes, Proposal, PullRequest
from tests.sv2_ctl02_cases import FIXTURE as AUTHORITY_FIXTURE
from tests.sv2_ctl02_cases import build as authority_fixture
from tests.test_swarm_v2_outcomes import Provider

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/outcome-attempts.json"


def build(monkeypatch, tmp_path):
    from tests.swarm_ledger.test_tasks import core, new_ledger

    inputs = json.loads(FIXTURE.read_text())
    store, authority, controller, clock, start = authority_fixture(monkeypatch)
    agent, token = start()
    claim = authority.admit(token, 30_000)
    repository = SQLiteLedgerRepository(tmp_path / "ledger.sqlite", core)
    row = {
        "id": agent.task,
        "title": "Outcome",
        "lane": "eng",
        "kind": "code",
        "phase": "p1",
        "state": "claimed",
        "done": False,
        "claimed_by": agent.name,
        "pr_url": inputs["pr_url"],
    }
    content = {
        "title": "Outcome fixture",
        "overview": "Final ownership",
        "sources": [],
        "phases": [{"title": "Controller", "description": "Final outcome"}],
        "tasks": [],
    }
    document = new_ledger.build_doc(content)
    document["tasks"] = [row]
    document, meta, _ = core.load_state(tmp_path / "seed.json", document)
    repository.create_document(authority.slug, document, meta)

    def read_task(task_id):
        return repository.get_document(authority.slug)["tasks"][0]

    proof = inputs["proof"]
    provider = Provider(PullRequest(inputs["pr_url"], "PR_one", inputs["head_sha"], "dev", "OPEN"))
    proposal = Proposal(
        agent.task, claim.generation, revision(read_task(agent.task)), inputs["pr_url"], inputs["head_sha"], proof
    )
    outcomes = Outcomes(authority, read_task, provider, lambda evidence: evidence == proof)
    return outcomes, repository, token, proposal, clock, start, agent


def replace_owner(outcomes, repository, proposal, clock, start, agent):
    clock[0] += 30_001
    successor, token = start(previous=agent.execution_id)
    claim = outcomes.authority.admit(token, 30_000)
    repository.apply_ops(
        outcomes.authority.slug,
        ops=[
            {
                "id": "replacement",
                "op": "task_update",
                "by": successor.name,
                "item": f"tasks/{proposal.task_id}",
                "fields": {"claimed_by": successor.name},
            }
        ],
    )
    proposal = replace(
        proposal, generation=claim.generation, task_revision=revision(outcomes.read_task(proposal.task_id))
    )
    return token, proposal


def positive(monkeypatch, tmp_path):
    outcomes, repository, old_token, proposal, clock, start, agent = build(monkeypatch, tmp_path)
    outcomes.propose(old_token, proposal)
    token, current = replace_owner(outcomes, repository, proposal, clock, start, agent)
    before = copy.deepcopy(repository.get_document(outcomes.authority.slug))
    with pytest.raises(SwarmError):
        outcomes.complete(old_token, proposal.generation, repository)
    assert repository.get_document(outcomes.authority.slug) == before
    outcomes.propose(token, current)
    outcomes.provider.lose_response = True
    with pytest.raises(SwarmError, match="not externally verified"):
        outcomes.complete(token, current.generation, repository)
    assert outcomes.read_task(current.task_id)["state"] == "claimed"
    result = outcomes.complete(token, current.generation, repository)
    assert outcomes.read_task(current.task_id)["state"] == "done"
    assert outcomes.authority.current(current.task_id).generation == 2
    assert outcomes.authority.current(current.task_id).state == "completed"
    assert result["merge_sha"] == "merged-commit"
    assert len(outcomes.provider.calls) == 1
    assert outcomes.outcome_conflicts_total() == 0
    return {
        "current_generation": 2,
        "ledger_done": True,
        "provider_dispatches": 1,
        "stale_ledger_changed": False,
        "outcome_conflicts_total": 0,
    }


def negative(monkeypatch, tmp_path):
    outcomes, repository, token, proposal, *_ = build(monkeypatch, tmp_path)
    claim = outcomes.authority.current(proposal.task_id)
    ledger = copy.deepcopy(repository.get_document(outcomes.authority.slug))
    invalid = replace(proposal, proof={"run": "success", "sha": proposal.head_sha})
    with pytest.raises(SwarmError, match="proof artifacts") as error:
        outcomes.propose(token, invalid)
    assert token not in str(error.value)
    assert outcomes.authority.current(proposal.task_id) == claim
    assert repository.get_document(outcomes.authority.slug) == ledger
    assert outcomes.provider.calls == []
    assert outcomes.outcome_conflicts_total() == 0
    outcomes.propose(token, proposal)
    assert outcomes.authority.current(proposal.task_id).result["phase"] == "accepted"
    return {
        "missing_artifact_rejected": True,
        "protected_state_changed": False,
        "provider_dispatches": 0,
        "new_valid_request_required": True,
        "outcome_conflicts_total": 0,
    }


def recovery(monkeypatch, tmp_path):
    outcomes, repository, old_token, proposal, clock, start, agent = build(monkeypatch, tmp_path)
    outcomes.propose(old_token, proposal)
    outcomes.provider.lose_response = True
    ambiguous = outcomes.integrate(old_token, proposal.generation)
    assert ambiguous["phase"] == "unknown"
    token, current = replace_owner(outcomes, repository, proposal, clock, start, agent)
    assert outcomes.authority.controller.release()
    controller = Controller(outcomes.authority.store, outcomes.authority.slug, [], lambda: True)
    assert controller.acquire()
    authority = TaskAuthority(outcomes.authority.store, controller, outcomes.authority.authorize)
    restored = Outcomes(authority, outcomes.read_task, outcomes.provider, outcomes.verify_proof)
    accepted = restored.propose(token, current)
    assert accepted["operation_id"] == ambiguous["operation_id"]
    result = restored.complete(token, current.generation, repository)
    assert result["merge_sha"] == "merged-commit"
    assert restored.read_task(current.task_id)["done"] is True
    assert len(restored.provider.calls) == 1
    before = copy.deepcopy(repository.get_document(authority.slug))
    with pytest.raises(SwarmError):
        restored.complete(old_token, proposal.generation, repository)
    assert repository.get_document(authority.slug) == before
    assert restored.outcome_conflicts_total() == 0
    return {
        "operation_identity_reused": True,
        "ledger_done": True,
        "provider_dispatches": 1,
        "old_owner_rejected": True,
        "outcome_conflicts_total": 0,
    }


def manifest():
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (FIXTURE, AUTHORITY_FIXTURE)}
