import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.swarm_v2.outcomes import GitHubIntegration, Outcomes, Proposal, PullRequest
from tests.sv2_ctl02_cases import build

pytestmark = pytest.mark.unit


class Provider:
    def __init__(self, pull):
        self.pull = pull
        self.calls = []
        self.lose_response = False

    def read(self, url):
        return self.pull

    def enqueue(self, pull, operation_id, guard):
        guard()
        self.calls.append(operation_id)
        self.pull = replace(pull, state="MERGED", merge_sha="merged-commit")
        if self.lose_response:
            raise TimeoutError
        return self.pull


@pytest.fixture
def fixture(monkeypatch):
    from scripts.swarm_ledger.api.resources import revision

    inputs = json.loads((Path(__file__).parent / "fixtures/swarm_v2/outcome-attempts.json").read_text())
    store, authority, controller, clock, start = build(monkeypatch)
    agent, token = start()
    claim = authority.admit(token, 30_000)
    task = {"id": agent.task, "kind": "code", "pr_url": inputs["pr_url"]}
    provider = Provider(PullRequest(task["pr_url"], "PR_one", inputs["head_sha"], "dev", "OPEN"))
    proof = inputs["proof"]
    proposal = Proposal(agent.task, claim.generation, revision(task), task["pr_url"], inputs["head_sha"], proof)
    outcomes = Outcomes(authority, lambda task_id: dict(task), provider, lambda evidence: evidence == proof)
    return outcomes, authority, token, proposal, provider, task, clock, start, agent


def test_current_worker_proposes_evidence_under_its_generation(fixture):
    outcomes, authority, token, proposal, provider, *_ = fixture
    accepted = outcomes.propose(token, proposal)
    assert accepted["phase"] == "accepted"
    assert accepted["provider_id"] == "PR_one"
    assert accepted["proposal"]["generation"] == 1
    assert authority.current(proposal.task_id).state == "active"
    assert provider.calls == []


def test_lost_merge_response_is_reconciled_before_any_retry(fixture):
    outcomes, authority, token, proposal, provider, *_ = fixture
    outcomes.propose(token, proposal)
    provider.lose_response = True
    ambiguous = outcomes.integrate(token, proposal.generation)
    assert ambiguous["phase"] == "unknown"
    assert authority.current(proposal.task_id).state == "active"
    recovered = outcomes.integrate(token, proposal.generation)
    assert recovered["phase"] == "externally_verified"
    assert recovered["merge_sha"] == "merged-commit"
    assert len(provider.calls) == 1
    assert authority.current(proposal.task_id).state == "active"


def test_replaced_attempt_cannot_propose_or_integrate(fixture):
    outcomes, authority, token, proposal, provider, task, clock, start, agent = fixture
    from scripts.swarm.store import SwarmError

    outcomes.propose(token, proposal)
    clock[0] += 30_001
    successor, current_token = start(previous=agent.execution_id)
    current = authority.admit(current_token, 30_000)
    with pytest.raises(SwarmError):
        outcomes.propose(token, proposal)
    with pytest.raises(SwarmError):
        outcomes.integrate(token, proposal.generation)
    outcomes.propose(current_token, replace(proposal, generation=current.generation))
    result = outcomes.integrate(current_token, current.generation)
    assert result["phase"] == "externally_verified"
    assert authority.current(proposal.task_id).execution_id == successor.execution_id
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    "proof",
    [
        {"run": "success", "sha": "tested-head"},
        {"artifact": "fixture://test-report", "sha": "tested-head"},
        {"artifact": "fixture://test-report", "run": "fixture-run", "sha": "wrong"},
        {"artifact": "unverified", "run": "fixture-run", "sha": "tested-head"},
    ],
)
def test_success_messages_without_verified_artifacts_cannot_propose(fixture, proof):
    outcomes, authority, token, proposal, provider, *_ = fixture
    from scripts.swarm.store import SwarmError

    with pytest.raises(SwarmError, match="proof artifacts"):
        outcomes.propose(token, replace(proposal, proof=proof))
    assert authority.current(proposal.task_id).result == {}
    assert provider.calls == []


def test_changed_task_revision_is_refused_at_final_boundary(fixture):
    outcomes, authority, token, proposal, provider, task, *_ = fixture
    from scripts.swarm.store import SwarmError

    outcomes.propose(token, proposal)
    task["description"] = "new instructions"
    with pytest.raises(SwarmError, match="outcome_conflict"):
        outcomes.integrate(token, proposal.generation)
    assert provider.calls == []
    assert authority.current(proposal.task_id).state == "active"
    assert outcomes.outcome_conflicts_total() == 1


def test_pause_retains_proposal_for_review(fixture):
    outcomes, authority, token, proposal, provider, *_ = fixture
    from scripts.swarm.store import SwarmError

    accepted = outcomes.propose(token, proposal)
    outcomes.integration_enabled = False
    with pytest.raises(SwarmError, match="paused"):
        outcomes.integrate(token, proposal.generation)
    assert authority.current(proposal.task_id).result == accepted
    assert provider.calls == []
    outcomes.integration_enabled = True
    assert outcomes.integrate(token, proposal.generation)["phase"] == "externally_verified"


def test_successor_reconciles_predecessors_ambiguous_effect(fixture):
    outcomes, authority, token, proposal, provider, task, clock, start, agent = fixture
    from scripts.swarm.store import SwarmError

    outcomes.propose(token, proposal)
    provider.lose_response = True
    prior = outcomes.integrate(token, proposal.generation)
    clock[0] += 30_001
    successor, current_token = start(previous=agent.execution_id)
    current = authority.admit(current_token, 30_000)
    accepted = outcomes.propose(current_token, replace(proposal, generation=current.generation))
    assert accepted["operation_id"] == prior["operation_id"]
    assert outcomes.integrate(current_token, current.generation)["phase"] == "externally_verified"
    assert len(provider.calls) == 1
    with pytest.raises(SwarmError):
        outcomes.integrate(token, proposal.generation)


def test_pending_ambiguous_operation_is_never_dispatched_twice(fixture):
    outcomes, authority, token, proposal, provider, *_ = fixture
    outcomes.propose(token, proposal)
    original = provider.enqueue

    def lose_without_observable_effect(pull, operation_id, guard):
        guard()
        provider.calls.append(operation_id)
        raise TimeoutError

    provider.enqueue = lose_without_observable_effect
    assert outcomes.integrate(token, proposal.generation)["phase"] == "unknown"
    provider.enqueue = original
    assert outcomes.integrate(token, proposal.generation)["phase"] == "unknown"
    assert len(provider.calls) == 1
    assert authority.current(proposal.task_id).state == "active"


def test_ownership_replacement_immediately_before_provider_write_refuses_it(fixture):
    outcomes, authority, token, proposal, provider, task, clock, start, agent = fixture
    from scripts.swarm.store import SwarmError

    outcomes.propose(token, proposal)
    enqueue = provider.enqueue

    def replace_before_write(pull, operation_id, guard):
        clock[0] += 30_001
        _, next_token = start(previous=agent.execution_id)
        authority.admit(next_token, 30_000)
        return enqueue(pull, operation_id, guard)

    provider.enqueue = replace_before_write
    with pytest.raises(SwarmError):
        outcomes.integrate(token, proposal.generation)
    assert provider.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", "untested-head"),
        ("url", "https://github.com/other/repo/pull/1"),
        ("base", "main"),
        ("identity", ""),
        ("state", "CLOSED"),
    ],
)
def test_changed_pr_identity_is_refused_before_provider_write(fixture, field, value):
    outcomes, authority, token, proposal, provider, *_ = fixture
    from scripts.swarm.store import SwarmError

    outcomes.propose(token, proposal)
    provider.pull = replace(provider.pull, **{field: value})
    with pytest.raises(SwarmError, match="outcome_conflict"):
        outcomes.integrate(token, proposal.generation)
    assert provider.calls == []


def test_queue_acceptance_is_committed_but_not_done(fixture):
    outcomes, authority, token, proposal, provider, *_ = fixture
    outcomes.propose(token, proposal)

    def queue(pull, operation_id, guard):
        guard()
        provider.calls.append(operation_id)
        provider.pull = replace(pull, queue_id="queue-entry")
        return provider.pull

    provider.enqueue = queue
    committed = outcomes.integrate(token, proposal.generation)
    assert committed["phase"] == "committed"
    assert committed["queue_id"] == "queue-entry"
    assert authority.current(proposal.task_id).state == "active"
    assert outcomes.integrate(token, proposal.generation) == committed
    assert len(provider.calls) == 1
    provider.pull = replace(provider.pull, state="MERGED", merge_sha="merged-commit")
    assert outcomes.integrate(token, proposal.generation)["phase"] == "externally_verified"


def test_github_provider_compares_the_head_and_guards_the_final_enqueue():
    url = "https://github.com/example/repo/pull/1"
    raw = {
        "id": "PR_one",
        "url": url,
        "headRefOid": "tested-head",
        "baseRefName": "dev",
        "state": "OPEN",
        "mergeQueueEntry": None,
        "mergeCommit": None,
    }
    calls = []

    def graphql(query, variables):
        if "mutation" in query:
            assert calls[-1] == "guard"
            assert variables == {"id": "PR_one", "head": "tested-head"}
            calls.append("enqueue")
            raw["mergeQueueEntry"] = {"id": "queue-entry"}
            return {}
        return {"resource": dict(raw)}

    provider = GitHubIntegration("example/repo", graphql)
    pull = provider.read(url)
    queued = provider.enqueue(pull, "operation", lambda: calls.append("guard"))
    assert queued.queue_id == "queue-entry"
    assert calls == ["guard", "enqueue"]
    raw["state"], raw["mergeCommit"] = "MERGED", {"oid": "merged-commit"}
    assert provider.read(url).merge_sha == "merged-commit"


def test_github_provider_refuses_foreign_repository_before_reading():
    from scripts.swarm.store import SwarmError

    provider = GitHubIntegration("example/repo", lambda query, variables: pytest.fail("foreign read"))
    with pytest.raises(SwarmError, match="forbidden_scope"):
        provider.read("https://github.com/other/repo/pull/1")


def test_github_provider_refuses_a_changed_head_before_guard_or_enqueue():
    from scripts.swarm.store import SwarmError

    url = "https://github.com/example/repo/pull/1"
    raw = {"id": "PR_one", "url": url, "headRefOid": "changed", "baseRefName": "dev", "state": "OPEN"}
    provider = GitHubIntegration("example/repo", lambda query, variables: {"resource": raw})
    with pytest.raises(SwarmError, match="outcome_conflict"):
        provider.enqueue(
            PullRequest(url, "PR_one", "tested-head", "dev", "OPEN"), "operation", lambda: pytest.fail("guard")
        )


def test_verified_completion_never_regresses_to_an_old_queue_observation(fixture):
    outcomes, authority, token, proposal, provider, *_ = fixture
    outcomes.propose(token, proposal)
    verified = outcomes.integrate(token, proposal.generation)
    provider.pull = replace(provider.pull, state="OPEN", queue_id="late-queue-entry", merge_sha="")
    assert outcomes.integrate(token, proposal.generation) == verified
    assert authority.current(proposal.task_id).result["phase"] == "externally_verified"


def test_pause_at_the_final_guard_keeps_the_proposal_retryable(fixture):
    outcomes, authority, token, proposal, provider, *_ = fixture
    from scripts.swarm.store import SwarmError

    outcomes.propose(token, proposal)
    original = provider.enqueue

    def pause_before_guard(pull, operation_id, guard):
        outcomes.integration_enabled = False
        return original(pull, operation_id, guard)

    provider.enqueue = pause_before_guard
    with pytest.raises(SwarmError, match="paused"):
        outcomes.integrate(token, proposal.generation)
    assert authority.current(proposal.task_id).result["phase"] == "accepted"
    assert provider.calls == []
    outcomes.integration_enabled = True
    provider.enqueue = original
    assert outcomes.integrate(token, proposal.generation)["phase"] == "externally_verified"
    assert len(provider.calls) == 1


def test_verified_outcome_completes_the_authoritative_ledger_once(fixture, tmp_path):
    from scripts.swarm_ledger.api.resources import revision
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
    from tests.swarm_ledger.test_tasks import core, new_ledger

    outcomes, authority, token, proposal, provider, task, clock, start, agent = fixture
    repository = SQLiteLedgerRepository(tmp_path / "ledger.sqlite", core)
    content = {
        "title": "Outcome fixture",
        "overview": "Final outcome",
        "sources": [],
        "phases": [{"id": "p1", "title": "Controller", "description": "d"}],
        "tasks": [
            {
                "id": proposal.task_id,
                "title": "Outcome",
                "lane": "eng",
                "kind": "code",
                "phase": "p1",
                "state": "claimed",
                "claimed_by": agent.name,
                "pr_url": proposal.pr_url,
            }
        ],
    }
    document = new_ledger.build_doc(content)
    document["tasks"] = content["tasks"]
    document, meta, _ = core.load_state(tmp_path / "seed.json", document)
    repository.create_document(authority.slug, document, meta)
    row = repository.get_document(authority.slug)["tasks"][0]
    outcomes.read_task = lambda task_id: repository.get_document(authority.slug)["tasks"][0]
    proposal = replace(proposal, task_revision=revision(row))
    outcomes.propose(token, proposal)
    result = outcomes.complete(token, proposal.generation, repository)
    state = repository.get_document(authority.slug)
    assert state["tasks"][0]["state"] == "done"
    assert state["tasks"][0]["done"] is True
    assert authority.current(proposal.task_id).state == "completed"
    assert state["tasks"][0]["proof"] == proposal.proof
    assert result["ledger_revision"] == state["_meta"]["rev"]
    prior = state
    replay = outcomes.complete(token, proposal.generation, repository)
    assert repository.get_document(authority.slug) == prior
    assert replay == result
    assert len(provider.calls) == 1


def test_ledger_completion_rejects_changed_revision_without_overwriting(fixture):
    outcomes, authority, token, proposal, provider, task, *_ = fixture
    outcomes.propose(token, proposal)
    outcomes.integrate(token, proposal.generation)
    task["description"] = "replacement instructions"
    from scripts.swarm_ledger import ledger_tasks
    from tests.swarm_ledger.test_tasks import core

    doc = {"tasks": [dict(task)], "phases": []}
    ctx = core.Context({"rev": 0, "stamps": {}}, 0)
    op = {
        "id": "completion",
        "op": "task_update",
        "item": f"tasks/{proposal.task_id}",
        "by": authority.current(proposal.task_id).holder,
        "fields": {"state": "done"},
    }
    before = json.loads(json.dumps(doc))
    assert not ledger_tasks.complete_outcome(doc, op, ctx, authority.current(proposal.task_id).result, op["by"])
    assert doc == before
    assert ctx.refused == ["outcome revision or ownership conflict"]


@pytest.mark.parametrize("case", ["positive", "negative", "recovery"])
def test_package_acceptance_cases_repeat_independently(monkeypatch, tmp_path, case):
    from tests import sv2_ctl05_cases

    run = getattr(sv2_ctl05_cases, case)
    first = run(monkeypatch, tmp_path / "first")
    second = run(monkeypatch, tmp_path / "second")
    assert first == second
    assert first["outcome_conflicts_total"] == 0
    assert set(sv2_ctl05_cases.manifest()) == {"outcome-attempts.json", "task-authority.json"}
