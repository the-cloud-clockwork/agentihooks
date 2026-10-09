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
        from scripts.swarm.store import SwarmError

        if url != self.pull.url:
            raise SwarmError("outcome_conflict")
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
    outcomes = Outcomes(
        authority,
        lambda task_id: dict(task) if task_id == task["id"] else {},
        provider,
        lambda evidence: evidence == proof,
    )
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
    with pytest.raises(SwarmError, match="^outcome_conflict$"):
        outcomes.integrate(token, proposal.generation)
    assert provider.calls == []
    assert authority.current(proposal.task_id).state == "active"
    assert outcomes.outcome_conflicts_total() == 1


def test_pause_retains_proposal_for_review(fixture):
    outcomes, authority, token, proposal, provider, *_ = fixture
    from scripts.swarm.store import SwarmError

    accepted = outcomes.propose(token, proposal)
    outcomes.integration_enabled = False
    with pytest.raises(SwarmError, match="^final integration is paused$"):
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
    with pytest.raises(SwarmError, match="^outcome_conflict$"):
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
        assert variables == {"url": url}
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
    with pytest.raises(SwarmError, match="^forbidden_scope$"):
        provider.read("https://github.com/other/repo/pull/1")


def test_github_provider_refuses_a_changed_head_before_guard_or_enqueue():
    from scripts.swarm.store import SwarmError

    url = "https://github.com/example/repo/pull/1"
    raw = {"id": "PR_one", "url": url, "headRefOid": "changed", "baseRefName": "dev", "state": "OPEN"}
    provider = GitHubIntegration("example/repo", lambda query, variables: {"resource": raw})
    with pytest.raises(SwarmError, match="^outcome_conflict$"):
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
    with pytest.raises(SwarmError, match="^final integration is paused$"):
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
    assert (
        state["_meta"]["outcomes"][proposal.task_id]["digest"]
        == "b6f69fb841b5bee5a01e3a6eef94590760133e5465e26a948cc84ea851cdc305"
    )
    assert state["_meta"]["outcomes"][proposal.task_id]["operation_id"] == result["operation_id"]
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
        "fields": {"state": "done", "pr_url": proposal.pr_url, "proof": proposal.proof},
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


def test_ledger_transaction_rolls_back_when_ownership_changes_at_the_final_check(monkeypatch, tmp_path):
    from scripts.swarm.store import SwarmError
    from tests import sv2_ctl05_cases

    outcomes, repository, token, proposal, clock, start, agent = sv2_ctl05_cases.build(monkeypatch, tmp_path)
    outcomes.propose(token, proposal)
    outcomes.integrate(token, proposal.generation)
    before = repository.get_document(outcomes.authority.slug)
    original = outcomes._receipt
    checks = []

    def replace_before_final_check(worker_token, generation, result):
        checks.append(generation)
        if len(checks) == 3:
            clock[0] += 30_001
            _, successor_token = start(previous=agent.execution_id)
            outcomes.authority.admit(successor_token, 30_000)
        return original(worker_token, generation, result)

    monkeypatch.setattr(outcomes, "_receipt", replace_before_final_check)
    with pytest.raises(SwarmError):
        outcomes.complete(token, proposal.generation, repository)
    assert repository.get_document(outcomes.authority.slug) == before
    assert outcomes.authority.current(proposal.task_id).generation == 2


def test_ledger_completion_refuses_tampered_payload(monkeypatch, tmp_path):
    from scripts.swarm_v2.outcomes import CompletionGate
    from tests import sv2_ctl05_cases

    outcomes, repository, token, proposal, *_ = sv2_ctl05_cases.build(monkeypatch, tmp_path)
    outcomes.propose(token, proposal)
    result = outcomes.integrate(token, proposal.generation)
    actor = outcomes.authority.current(proposal.task_id).holder
    before = repository.get_document(outcomes.authority.slug)["tasks"][0]
    operation = {
        "id": "tampered",
        "op": "task_update",
        "item": f"tasks/{proposal.task_id}",
        "by": actor,
        "fields": {"state": "done", "pr_url": proposal.pr_url, "proof": {"run": "success"}},
    }
    _, rejected = repository.apply_ops(
        outcomes.authority.slug, ops=[operation], gate=CompletionGate(outcomes, token, proposal.generation, result)
    )
    assert rejected == ["tampered"]
    assert repository.get_document(outcomes.authority.slug)["tasks"][0] == before
    assert outcomes.authority.current(proposal.task_id).state == "active"


def test_interrupted_authority_completion_replays_the_committed_ledger(monkeypatch, tmp_path):
    from tests import sv2_ctl05_cases

    outcomes, repository, token, proposal, *_ = sv2_ctl05_cases.build(monkeypatch, tmp_path)
    outcomes.propose(token, proposal)
    original = outcomes.authority.complete

    def interrupt(worker_token, generation, result):
        raise ConnectionError("interrupted after ledger commit")

    monkeypatch.setattr(outcomes.authority, "complete", interrupt)
    with pytest.raises(ConnectionError, match="after ledger commit"):
        outcomes.complete(token, proposal.generation, repository)
    before = repository.get_document(outcomes.authority.slug)
    assert before["tasks"][0]["done"] is True
    assert outcomes.authority.current(proposal.task_id).state == "active"
    monkeypatch.setattr(outcomes.authority, "complete", original)
    completed = outcomes.complete(token, proposal.generation, repository)
    assert completed["ledger_revision"] == before["_meta"]["rev"]
    assert repository.get_document(outcomes.authority.slug) == before
    assert outcomes.authority.current(proposal.task_id).state == "completed"
    assert len(outcomes.provider.calls) == 1


def test_successor_finishes_authority_after_a_committed_ledger_response_is_lost(monkeypatch, tmp_path):
    from scripts.swarm_ledger.api.resources import revision
    from tests import sv2_ctl05_cases

    outcomes, repository, token, proposal, clock, start, agent = sv2_ctl05_cases.build(monkeypatch, tmp_path)
    outcomes.propose(token, proposal)
    original = outcomes.authority.complete

    def interrupt(worker_token, generation, result):
        raise ConnectionError("interrupted authority acknowledgement")

    monkeypatch.setattr(outcomes.authority, "complete", interrupt)
    with pytest.raises(ConnectionError):
        outcomes.complete(token, proposal.generation, repository)
    before = repository.get_document(outcomes.authority.slug)
    clock[0] += 30_001
    _, successor_token = start(previous=agent.execution_id)
    current = outcomes.authority.admit(successor_token, 30_000)
    successor = replace(proposal, generation=current.generation, task_revision=revision(before["tasks"][0]))
    accepted = outcomes.propose(successor_token, successor)
    monkeypatch.setattr(outcomes.authority, "complete", original)
    completed = outcomes.complete(successor_token, current.generation, repository)
    assert completed["operation_id"] == accepted["operation_id"]
    assert repository.get_document(outcomes.authority.slug) == before
    assert outcomes.authority.current(proposal.task_id).generation == 2
    assert outcomes.authority.current(proposal.task_id).state == "completed"
    assert len(outcomes.provider.calls) == 1


def test_proposal_replay_keeps_one_accepted_journal_entry(fixture):
    outcomes, authority, token, proposal, *_ = fixture
    first = outcomes.propose(token, proposal)
    journal = authority.journal(proposal.task_id)
    assert outcomes.propose(token, proposal) == first
    assert authority.journal(proposal.task_id) == journal
    assert journal[-1]["event"] == "outcome_proposed"


def test_integration_requires_an_accepted_proposal(fixture):
    from scripts.swarm.store import SwarmError

    outcomes, authority, token, proposal, provider, *_ = fixture
    with pytest.raises(SwarmError, match="^an accepted outcome is required$"):
        outcomes.integrate(token, proposal.generation)
    assert provider.calls == []


@pytest.mark.parametrize(
    "proof",
    [
        {"artifact": "fixture://test-report", "sha": "tested-head"},
        {"artifact": "fixture://test-report", "run": "fixture-run", "sha": "wrong"},
    ],
)
def test_required_proof_fields_are_checked_before_the_artifact_verifier(fixture, proof):
    from scripts.swarm.store import SwarmError

    outcomes, authority, token, proposal, *_ = fixture
    outcomes.verify_proof = lambda evidence: True
    with pytest.raises(SwarmError, match="^required proof artifacts are absent or unverified$"):
        outcomes.propose(token, replace(proposal, proof=proof))
    assert authority.current(proposal.task_id).result == {}


def test_expired_owner_cannot_reconcile_an_unchanged_unknown_outcome(fixture):
    from scripts.swarm.store import SwarmError

    outcomes, authority, token, proposal, provider, task, clock, *_ = fixture
    outcomes.propose(token, proposal)

    def lost(pull, operation_id, guard):
        guard()
        raise TimeoutError

    provider.enqueue = lost
    assert outcomes.integrate(token, proposal.generation)["phase"] == "unknown"
    before = authority.current(proposal.task_id)
    clock[0] += 30_001
    with pytest.raises(SwarmError, match="^stale_generation$"):
        outcomes.integrate(token, proposal.generation)
    assert authority.current(proposal.task_id) == before


def test_reconciliation_journals_exact_phase_events(fixture):
    outcomes, authority, token, proposal, provider, *_ = fixture
    outcomes.propose(token, proposal)
    outcomes.integrate(token, proposal.generation)
    events = [row["event"] for row in authority.journal(proposal.task_id)]
    assert events == ["admitted", "outcome_proposed", "outcome_reconciled", "outcome_reconciled"]


def test_github_provider_rejects_a_non_pr_resource_with_its_exact_error():
    from scripts.swarm.store import SwarmError

    provider = GitHubIntegration("example/repo", lambda query, variables: {"resource": None})
    with pytest.raises(SwarmError, match="^GitHub URL does not identify a pull request$"):
        provider.read("https://github.com/example/repo/pull/1")


def test_github_provider_reports_absent_commit_identity_as_empty():
    raw = {
        "id": "PR_one",
        "url": "https://github.com/example/repo/pull/1",
        "headRefOid": "tested-head",
        "baseRefName": "dev",
        "state": "OPEN",
        "mergeCommit": {},
    }
    provider = GitHubIntegration("example/repo", lambda query, variables: {"resource": raw})
    assert provider.read(raw["url"]) == PullRequest(raw["url"], "PR_one", "tested-head", "dev", "OPEN")


def test_paused_verified_outcome_cannot_mark_the_ledger_done(monkeypatch, tmp_path):
    from scripts.swarm.store import SwarmError
    from tests import sv2_ctl05_cases

    outcomes, repository, token, proposal, *_ = sv2_ctl05_cases.build(monkeypatch, tmp_path)
    outcomes.propose(token, proposal)
    outcomes.integrate(token, proposal.generation)
    before = repository.get_document(outcomes.authority.slug)
    outcomes.integration_enabled = False
    with pytest.raises(SwarmError, match="^final integration is paused$"):
        outcomes.complete(token, proposal.generation, repository)
    assert repository.get_document(outcomes.authority.slug) == before


def test_scope_changed_during_final_verification_is_refused(monkeypatch, tmp_path):
    from scripts.swarm.store import SwarmError
    from tests import sv2_ctl05_cases

    outcomes, repository, token, proposal, *_ = sv2_ctl05_cases.build(monkeypatch, tmp_path)
    outcomes.propose(token, proposal)
    outcomes.integrate(token, proposal.generation)
    original = outcomes.authority.authorize
    changed = []

    def authorize(worker_token):
        scope = original(worker_token)
        return replace(scope, project_ids=[]) if changed else scope

    def verify(proof):
        changed.append(True)
        return proof == proposal.proof

    outcomes.authority.authorize = authorize
    outcomes.verify_proof = verify
    before = repository.get_document(outcomes.authority.slug)
    with pytest.raises(SwarmError, match="^forbidden_scope$"):
        outcomes.complete(token, proposal.generation, repository)
    assert repository.get_document(outcomes.authority.slug) == before


def test_changed_revision_refusal_uses_the_ledger_operation_identity(monkeypatch, tmp_path):
    from scripts.swarm.store import SwarmError
    from tests import sv2_ctl05_cases

    outcomes, repository, token, proposal, *_ = sv2_ctl05_cases.build(monkeypatch, tmp_path)
    outcomes.propose(token, proposal)
    outcomes.integrate(token, proposal.generation)
    repository.apply_ops(
        outcomes.authority.slug,
        ops=[
            {
                "id": "instructions",
                "op": "task_update",
                "item": f"tasks/{proposal.task_id}",
                "by": outcomes.authority.current(proposal.task_id).holder,
                "fields": {"description": "new instructions"},
            }
        ],
    )
    before = repository.get_document(outcomes.authority.slug)["tasks"][0]
    with pytest.raises(SwarmError, match="^the ledger outcome was refused$"):
        outcomes.complete(token, proposal.generation, repository)
    assert repository.get_document(outcomes.authority.slug)["tasks"][0] == before
    assert outcomes.authority.current(proposal.task_id).state == "active"


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "open"),
        ("proof", {"run": "success"}),
        ("pr_url", "https://github.com/example/repo/pull/2"),
        ("claimed_by", "replacement"),
    ],
)
def test_committed_outcome_fields_cannot_be_rewritten_by_legacy_updates(monkeypatch, tmp_path, field, value):
    from tests import sv2_ctl05_cases

    outcomes, repository, token, proposal, *_ = sv2_ctl05_cases.build(monkeypatch, tmp_path)
    outcomes.propose(token, proposal)
    outcomes.complete(token, proposal.generation, repository)
    before = repository.get_document(outcomes.authority.slug)["tasks"][0]
    _, rejected = repository.apply_ops(
        outcomes.authority.slug,
        ops=[
            {
                "id": "legacy-rewrite",
                "op": "task_update",
                "item": f"tasks/{proposal.task_id}",
                "by": outcomes.authority.current(proposal.task_id).holder,
                "fields": {field: value},
            }
        ],
    )
    assert rejected == ["legacy-rewrite"]
    assert repository.get_document(outcomes.authority.slug)["tasks"][0] == before


def test_committed_outcome_allows_an_unprotected_description_update(monkeypatch, tmp_path):
    from tests import sv2_ctl05_cases

    outcomes, repository, token, proposal, *_ = sv2_ctl05_cases.build(monkeypatch, tmp_path)
    outcomes.propose(token, proposal)
    outcomes.complete(token, proposal.generation, repository)
    state, rejected = repository.apply_ops(
        outcomes.authority.slug,
        ops=[
            {
                "id": "description",
                "op": "task_update",
                "item": f"tasks/{proposal.task_id}",
                "by": outcomes.authority.current(proposal.task_id).holder,
                "fields": {"description": "verified result"},
            }
        ],
    )
    assert rejected == []
    assert state["tasks"][0]["description"] == "verified result"


def test_changed_committed_effect_cannot_reuse_its_ledger_receipt(monkeypatch, tmp_path):
    from copy import deepcopy

    from scripts.swarm_ledger import ledger_tasks
    from tests import sv2_ctl05_cases
    from tests.swarm_ledger.test_tasks import core

    outcomes, repository, token, proposal, *_ = sv2_ctl05_cases.build(monkeypatch, tmp_path)
    outcomes.propose(token, proposal)
    result = outcomes.complete(token, proposal.generation, repository)
    source = repository.get_document(outcomes.authority.slug)
    doc = deepcopy(source)
    ctx = core.Context(doc.pop("_meta"), 0)
    changed = {**result, "merge_sha": "another-commit"}
    operation = {
        "id": "different-effect",
        "op": "task_update",
        "item": f"tasks/{proposal.task_id}",
        "by": outcomes.authority.current(proposal.task_id).holder,
        "fields": {"state": "done", "pr_url": proposal.pr_url, "proof": proposal.proof},
    }
    before = deepcopy(doc)
    assert not ledger_tasks.complete_outcome(doc, operation, ctx, changed, operation["by"])
    assert ctx.refused == ["outcome receipt conflict"]
    assert doc == before


@pytest.mark.parametrize("field,value", [("op", "task_add"), ("item", "tasks/other"), ("by", "forged")])
def test_completion_refuses_an_operation_of_another_kind(monkeypatch, tmp_path, field, value):
    from copy import deepcopy

    from scripts.swarm_ledger import ledger_tasks
    from tests import sv2_ctl05_cases
    from tests.swarm_ledger.test_tasks import core

    outcomes, repository, token, proposal, *_ = sv2_ctl05_cases.build(monkeypatch, tmp_path)
    outcomes.propose(token, proposal)
    result = outcomes.integrate(token, proposal.generation)
    doc = repository.get_document(outcomes.authority.slug)
    ctx = core.Context(doc.pop("_meta"), 0)
    operation = {
        "id": "wrong-kind",
        "op": "task_update",
        "item": f"tasks/{proposal.task_id}",
        "by": outcomes.authority.current(proposal.task_id).holder,
        "fields": {"state": "done", "pr_url": proposal.pr_url, "proof": proposal.proof},
    }
    operation[field] = value
    before = deepcopy(doc)
    assert not ledger_tasks.complete_outcome(
        doc, operation, ctx, result, outcomes.authority.current(proposal.task_id).holder
    )
    assert ctx.refused == ["outcome identity conflict"]
    assert doc == before


def test_replacement_during_final_proof_verification_rolls_back_the_ledger(monkeypatch, tmp_path):
    from scripts.swarm.store import SwarmError
    from tests import sv2_ctl05_cases

    outcomes, repository, token, proposal, clock, start, agent = sv2_ctl05_cases.build(monkeypatch, tmp_path)
    outcomes.propose(token, proposal)
    outcomes.integrate(token, proposal.generation)
    before = repository.get_document(outcomes.authority.slug)
    checks = []

    def verify(proof):
        checks.append(proof)
        if len(checks) == 3:
            clock[0] += 30_001
            _, replacement_token = start(previous=agent.execution_id)
            outcomes.authority.admit(replacement_token, 30_000)
        return proof == proposal.proof

    outcomes.verify_proof = verify
    with pytest.raises(SwarmError):
        outcomes.complete(token, proposal.generation, repository)
    assert repository.get_document(outcomes.authority.slug) == before
    assert outcomes.authority.current(proposal.task_id).generation == 2


def test_proposal_task_scope_is_refused_exactly(fixture):
    from scripts.swarm.store import SwarmError

    outcomes, authority, token, proposal, provider, *_ = fixture
    with pytest.raises(SwarmError, match="^forbidden_scope$"):
        outcomes.propose(token, replace(proposal, task_id="another-task"))
    assert authority.current(proposal.task_id).result == {}
    assert provider.calls == []


def test_independent_generations_receive_distinct_operation_identities(fixture):
    outcomes, authority, token, proposal, provider, task, clock, start, agent = fixture
    first = outcomes.propose(token, proposal)
    clock[0] += 30_001
    _, successor_token = start(previous=agent.execution_id)
    claim = authority.admit(successor_token, 30_000)
    second = outcomes.propose(successor_token, replace(proposal, generation=claim.generation))
    assert first["operation_id"] != second["operation_id"]
    assert provider.calls == []


def test_successor_inherits_a_queued_effect_past_an_earlier_accepted_proposal(fixture, monkeypatch):
    outcomes, authority, token, proposal, provider, task, clock, start, agent = fixture
    outcomes.propose(token, proposal)
    clock[0] += 30_001
    successor, next_token = start(previous=agent.execution_id)
    claim = authority.admit(next_token, 30_000)
    second = replace(proposal, generation=claim.generation)
    outcomes.propose(next_token, second)

    def enqueue(pull, operation_id, guard):
        guard()
        provider.calls.append(operation_id)
        provider.pull = replace(pull, queue_id="queued-once")
        return provider.pull

    monkeypatch.setattr(provider, "enqueue", enqueue)
    queued = outcomes.integrate(next_token, claim.generation)
    assert queued["phase"] == "committed"
    clock[0] += 30_001
    _, final_token = start(previous=successor.execution_id)
    final = authority.admit(final_token, 30_000)
    inherited = outcomes.propose(final_token, replace(proposal, generation=final.generation))
    assert inherited["operation_id"] == queued["operation_id"]
    assert inherited["phase"] == "committed"
    assert outcomes.integrate(final_token, final.generation)["queue_id"] == "queued-once"
    assert provider.calls == [queued["operation_id"]]


def test_dispatch_identity_is_present_and_bound_to_the_provider_operation(fixture):
    outcomes, authority, token, proposal, provider, *_ = fixture
    accepted = outcomes.propose(token, proposal)
    provider.lose_response = True
    unknown = outcomes.integrate(token, proposal.generation)
    assert isinstance(unknown["dispatch"], str)
    assert len(unknown["dispatch"]) == 32
    assert provider.calls == [accepted["operation_id"]]


def test_final_guard_refuses_an_operation_already_admitted_to_the_queue(fixture, monkeypatch):
    from scripts.swarm.store import SwarmError

    outcomes, authority, token, proposal, provider, *_ = fixture
    outcomes.propose(token, proposal)

    def enqueue(pull, operation_id, guard):
        provider.pull = replace(pull, queue_id="another-response")
        assert outcomes.integrate(token, proposal.generation)["phase"] == "committed"
        guard()
        provider.calls.append(operation_id)
        return provider.pull

    monkeypatch.setattr(provider, "enqueue", enqueue)
    with pytest.raises(SwarmError, match="^outcome_conflict$"):
        outcomes.integrate(token, proposal.generation)
    assert provider.calls == []
    assert outcomes.outcome_conflicts_total() == 1


def test_revoked_proof_cannot_complete_an_already_verified_outcome(monkeypatch, tmp_path):
    from scripts.swarm.store import SwarmError
    from tests import sv2_ctl05_cases

    outcomes, repository, token, proposal, *_ = sv2_ctl05_cases.build(monkeypatch, tmp_path)
    outcomes.propose(token, proposal)
    outcomes.integrate(token, proposal.generation)
    outcomes.verify_proof = lambda proof: False
    before = repository.get_document(outcomes.authority.slug)
    with pytest.raises(SwarmError, match="^required proof artifacts are absent or unverified$"):
        outcomes.complete(token, proposal.generation, repository)
    assert repository.get_document(outcomes.authority.slug) == before


def test_completion_without_observed_merge_uses_the_exact_refusal(monkeypatch, tmp_path):
    from scripts.swarm.store import SwarmError
    from tests import sv2_ctl05_cases

    outcomes, repository, token, proposal, *_ = sv2_ctl05_cases.build(monkeypatch, tmp_path)
    outcomes.propose(token, proposal)
    outcomes.provider.lose_response = True
    with pytest.raises(SwarmError, match="^the provider outcome is not externally verified$"):
        outcomes.complete(token, proposal.generation, repository)
    assert outcomes.read_task(proposal.task_id)["state"] == "claimed"


def test_completion_during_provider_observation_refuses_the_missing_proposal(fixture, monkeypatch):
    from scripts.swarm.store import SwarmError

    outcomes, authority, token, proposal, provider, *_ = fixture
    outcomes.propose(token, proposal)
    original = provider.read

    def read(url):
        authority.complete(token, proposal.generation, {})
        return original(url)

    monkeypatch.setattr(provider, "read", read)
    with pytest.raises(SwarmError, match="^an accepted outcome is required$"):
        outcomes.integrate(token, proposal.generation)
    assert provider.calls == []


def test_reconciliation_after_ledger_completion_checks_the_current_revision(monkeypatch, tmp_path):
    from scripts.swarm.store import SwarmError
    from tests import sv2_ctl05_cases

    outcomes, repository, token, proposal, *_ = sv2_ctl05_cases.build(monkeypatch, tmp_path)
    outcomes.propose(token, proposal)
    outcomes.complete(token, proposal.generation, repository)
    with pytest.raises(SwarmError, match="^outcome_conflict$"):
        outcomes.integrate(token, proposal.generation)
    assert outcomes.read_task(proposal.task_id)["state"] == "done"


@pytest.mark.parametrize(
    "change",
    [
        {"claimed_by": "another-owner"},
        {"pr_url": "https://github.com/example/repo/pull/2"},
        {"phase": "committed"},
        {"merge_sha": ""},
        {"missing_task": True},
    ],
)
def test_authoritative_completion_refuses_incomplete_or_mismatched_effects(monkeypatch, tmp_path, change):
    from copy import deepcopy

    from scripts.swarm_ledger import ledger_tasks
    from scripts.swarm_ledger.api.resources import revision
    from tests import sv2_ctl05_cases
    from tests.swarm_ledger.test_tasks import core

    outcomes, repository, token, proposal, *_ = sv2_ctl05_cases.build(monkeypatch, tmp_path)
    outcomes.propose(token, proposal)
    result = outcomes.integrate(token, proposal.generation)
    doc = repository.get_document(outcomes.authority.slug)
    ctx = core.Context(doc.pop("_meta"), 0)
    actor = outcomes.authority.current(proposal.task_id).holder
    if change.get("missing_task"):
        doc["tasks"] = []
        expected = "outcome identity conflict"
    else:
        for key, value in change.items():
            if key in ("claimed_by", "pr_url"):
                doc["tasks"][0][key] = value
                result["proposal"]["task_revision"] = revision(doc["tasks"][0])
            else:
                result[key] = value
        expected = "outcome revision or ownership conflict"
    operation = {
        "id": "invalid-effect",
        "op": "task_update",
        "item": f"tasks/{proposal.task_id}",
        "by": actor,
        "fields": {"state": "done", "pr_url": proposal.pr_url, "proof": proposal.proof},
    }
    before = deepcopy(doc)
    assert not ledger_tasks.complete_outcome(doc, operation, ctx, result, actor)
    assert ctx.refused == [expected]
    assert doc == before


def test_committed_outcome_refusal_reports_the_exact_controller_requirement(monkeypatch, tmp_path):
    from scripts.swarm_ledger import ledger_tasks
    from tests import sv2_ctl05_cases

    outcomes, repository, token, proposal, *_ = sv2_ctl05_cases.build(monkeypatch, tmp_path)
    outcomes.propose(token, proposal)
    outcomes.complete(token, proposal.generation, repository)
    doc = repository.get_document(outcomes.authority.slug)
    operation = {"item": f"tasks/{proposal.task_id}", "fields": {"state": "open"}}
    assert (
        ledger_tasks.update_refusal(doc, operation, doc["_meta"])
        == "committed outcomes require controller reconciliation"
    )


def test_outcome_receipt_is_dirty_even_when_done_fields_are_already_present(monkeypatch, tmp_path):
    from scripts.swarm_ledger import ledger_tasks
    from scripts.swarm_ledger.api.resources import revision
    from tests import sv2_ctl05_cases
    from tests.swarm_ledger.test_tasks import core

    outcomes, repository, token, proposal, *_ = sv2_ctl05_cases.build(monkeypatch, tmp_path)
    outcomes.propose(token, proposal)
    result = outcomes.integrate(token, proposal.generation)
    doc = repository.get_document(outcomes.authority.slug)
    ctx = core.Context(doc.pop("_meta"), 0)
    actor = outcomes.authority.current(proposal.task_id).holder
    fields = {"state": "done", "pr_url": proposal.pr_url, "proof": proposal.proof}
    doc["tasks"][0].update(fields)
    doc["tasks"][0]["done"] = True
    result["proposal"]["task_revision"] = revision(doc["tasks"][0])
    operation = {
        "id": "receipt",
        "op": "task_update",
        "item": f"tasks/{proposal.task_id}",
        "by": actor,
        "fields": fields,
    }
    assert ledger_tasks.complete_outcome(doc, operation, ctx, result, actor)
    assert ctx.dirty is True
    assert ctx.meta["outcomes"][proposal.task_id]["operation_id"] == result["operation_id"]


def test_ledger_proof_contract_refusal_is_not_committed_as_an_outcome(monkeypatch, tmp_path):
    from copy import deepcopy

    from scripts.swarm_ledger import ledger_tasks
    from scripts.swarm_ledger.api.resources import revision
    from tests import sv2_ctl05_cases
    from tests.swarm_ledger.test_tasks import core

    outcomes, repository, token, proposal, *_ = sv2_ctl05_cases.build(monkeypatch, tmp_path)
    outcomes.propose(token, proposal)
    result = outcomes.integrate(token, proposal.generation)
    doc = repository.get_document(outcomes.authority.slug)
    ctx = core.Context(doc.pop("_meta"), 0)
    actor = outcomes.authority.current(proposal.task_id).holder
    doc["tasks"][0]["kind"] = "ops"
    result["proposal"]["task_revision"] = revision(doc["tasks"][0])
    operation = {
        "id": "missing-command",
        "op": "task_update",
        "item": f"tasks/{proposal.task_id}",
        "by": actor,
        "fields": {"state": "done", "pr_url": proposal.pr_url, "proof": proposal.proof},
    }
    before = deepcopy(doc)
    assert not ledger_tasks.complete_outcome(doc, operation, ctx, result, actor)
    assert ctx.refused
    assert "outcomes" not in ctx.meta
    assert ctx.dirty is False
    assert doc == before
