import json
import re
from dataclasses import asdict, dataclass, replace
from typing import Callable, Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from scripts.swarm.store import SwarmError
from scripts.swarm_ledger.api.resources import revision
from scripts.swarm_v2.authority import TaskAuthority


@dataclass(frozen=True)
class Proposal:
    task_id: str
    generation: int
    task_revision: str
    pr_url: str
    head_sha: str
    proof: dict


@dataclass(frozen=True)
class PullRequest:
    url: str
    identity: str
    head_sha: str
    base: str
    state: str
    queue_id: str = ""
    merge_sha: str = ""


class IntegrationProvider(Protocol):
    def read(self, url: str) -> PullRequest: ...

    def enqueue(self, pull: PullRequest, operation_id: str, guard: Callable[[], None]) -> PullRequest:
        """Invoke guard immediately before the provider mutation and compare the observed head."""
        ...


class GitHubIntegration:
    def __init__(self, repository: str, graphql: Callable[[str, dict], dict]) -> None:
        self.repository, self.graphql = repository, graphql

    def read(self, url: str) -> PullRequest:
        if not re.fullmatch(rf"https://github\.com/{re.escape(self.repository)}/pull/[1-9][0-9]*", url):
            raise SwarmError("forbidden_scope")
        query = """query($url:URI!){resource(url:$url){... on PullRequest{
            id url headRefOid baseRefName state mergeQueueEntry{id} mergeCommit{oid}
        }}}"""
        raw = self.graphql(query, {"url": url})["resource"]
        if not raw:
            raise SwarmError("GitHub URL does not identify a pull request")
        return PullRequest(
            raw["url"],
            raw["id"],
            raw["headRefOid"],
            raw["baseRefName"],
            raw["state"],
            (raw.get("mergeQueueEntry") or {}).get("id", ""),
            (raw.get("mergeCommit") or {}).get("oid", ""),
        )

    def enqueue(self, pull: PullRequest, operation_id: str, guard: Callable[[], None]) -> PullRequest:
        from scripts.swarm.merge_queue import QUEUE

        if self.read(pull.url) != pull or pull.base != "dev":
            raise SwarmError("outcome_conflict")
        guard()
        self.graphql(QUEUE, {"id": pull.identity, "head": pull.head_sha})
        return self.read(pull.url)


class Outcomes:
    def __init__(
        self,
        authority: TaskAuthority,
        read_task: Callable[[str], dict],
        provider: IntegrationProvider,
        verify_proof: Callable[[dict], bool],
        integration_enabled: bool = True,
    ) -> None:
        self.authority, self.read_task = authority, read_task
        self.provider, self.verify_proof = provider, verify_proof
        self.integration_enabled = integration_enabled

    def propose(self, token: str, proposal: Proposal) -> dict:
        pull = self.provider.read(proposal.pr_url)

        def accept(pipe, scope, previous):
            if scope.task_id != proposal.task_id:
                raise SwarmError("forbidden_scope")
            self.authority._holder(scope, proposal.generation, previous)
            self._validate(proposal, pull)
            operation_id = uuid5(
                NAMESPACE_URL,
                revision([self.authority.slug, proposal.task_id, proposal.generation, pull.identity, pull.head_sha]),
            ).hex
            outcome = {
                "phase": "accepted",
                "proposal": asdict(proposal),
                "operation_id": operation_id,
                "provider_id": pull.identity,
            }
            if previous.result:
                if previous.result.get("proposal") != outcome["proposal"]:
                    self._conflict()
                return previous
            return replace(previous, result=self._inherit(pipe, proposal.task_id, outcome))

        return self.authority._write(token, "outcome_proposed", accept).result

    def _inherit(self, pipe, task_id: str, outcome: dict) -> dict:
        store = self.authority.store
        operations = {}
        for raw in pipe.lrange(store.key(self.authority.slug, "claim-journal", task_id), 0, -1):
            prior = json.loads(raw)["claim"]["result"]
            if prior.get("operation_id"):
                operations[prior["operation_id"]] = prior
        for prior in operations.values():
            if prior["phase"] not in ("unknown", "committed"):
                continue
            if (prior["provider_id"], prior["proposal"]["head_sha"], prior["proposal"]["pr_url"]) != (
                outcome["provider_id"],
                outcome["proposal"]["head_sha"],
                outcome["proposal"]["pr_url"],
            ):
                self._conflict()
            outcome = {**prior, "proposal": outcome["proposal"]}
        return outcome

    def integrate(self, token: str, generation: int) -> dict:
        scope = self.authority._scope(token)
        claim = self.authority.current(scope.task_id)
        self.authority.controller.require()
        self.authority._identity(scope, generation, claim)
        if not claim.result:
            raise SwarmError("an accepted outcome is required")
        proposal = Proposal(**claim.result["proposal"])
        pull = self.provider.read(proposal.pr_url)
        outcome = self._settle(token, generation, pull)
        if outcome["phase"] != "accepted":
            return outcome
        if not self.integration_enabled:
            raise SwarmError("final integration is paused")
        dispatch = uuid4().hex

        def reserve(previous):
            if previous["phase"] != "accepted":
                return previous
            return {**previous, "phase": "unknown", "dispatch": dispatch}

        outcome = self._update(token, generation, pull, reserve)
        if outcome.get("dispatch") != dispatch:
            return outcome

        def guard():
            checked = self._update(token, generation, pull, lambda previous: previous)
            if checked.get("dispatch") != dispatch or checked["phase"] != "unknown":
                self._conflict()
            if not self.integration_enabled:
                self._update(token, generation, pull, lambda previous: {**previous, "phase": "accepted"})
                raise SwarmError("final integration is paused")

        try:
            observed = self.provider.enqueue(pull, outcome["operation_id"], guard)
        except (TimeoutError, ConnectionError):
            return outcome
        return self._settle(token, generation, observed)

    def complete(self, token: str, generation: int, repository) -> dict:
        scope = self.authority._scope(token)
        claim = self.authority.current(scope.task_id)
        self.authority.controller.require()
        self.authority._identity(scope, generation, claim)
        outcome = claim.result if claim.state == "completed" else self.integrate(token, generation)
        if outcome.get("phase") != "externally_verified":
            raise SwarmError("the provider outcome is not externally verified")
        claim = self._receipt(token, generation, outcome)
        gate = CompletionGate(self, token, generation, outcome)
        proposal = outcome["proposal"]
        op = {
            "id": f"outcome-{outcome['operation_id']}",
            "op": "task_update",
            "item": f"tasks/{proposal['task_id']}",
            "by": claim.holder,
            "fields": {"state": "done", "pr_url": proposal["pr_url"], "proof": proposal["proof"]},
        }
        state, rejected = repository.apply_ops(self.authority.slug, ops=[op], gate=gate)
        if rejected:
            raise SwarmError("the ledger outcome was refused")
        return {**outcome, "ledger_revision": state["_meta"]["rev"]}

    def _receipt(self, token: str, generation: int, outcome: dict):
        def check(pipe, scope, previous):
            self.authority._identity(scope, generation, previous)
            if previous.state != "completed" or previous.result != outcome:
                self._conflict()
            proof = outcome["proposal"]["proof"]
            if self.verify_proof(proof) is not True:
                raise SwarmError("required proof artifacts are absent or unverified")
            return previous

        return self.authority._write(token, "outcome_checked", check)

    def _settle(self, token: str, generation: int, pull: PullRequest) -> dict:
        def settle(previous):
            if pull.identity != previous["provider_id"]:
                self._conflict()
            if previous["phase"] == "externally_verified":
                return previous
            if pull.state == "MERGED":
                if not pull.merge_sha:
                    self._conflict()
                return {**previous, "phase": "externally_verified", "merge_sha": pull.merge_sha}
            if pull.state != "OPEN":
                self._conflict()
            if pull.queue_id:
                return {**previous, "phase": "committed", "queue_id": pull.queue_id}
            return previous

        return self._update(token, generation, pull, settle)

    def _update(self, token: str, generation: int, pull: PullRequest, change: Callable[[dict], dict]) -> dict:
        def update(pipe, scope, previous):
            self.authority._identity(scope, generation, previous)
            if previous.state != "completed":
                self.authority._holder(scope, generation, previous)
            if not previous.result:
                raise SwarmError("an accepted outcome is required")
            self._validate(Proposal(**previous.result["proposal"]), pull)
            outcome = change(previous.result)
            state = "completed" if outcome["phase"] == "externally_verified" else previous.state
            return replace(previous, result=outcome, state=state)

        return self.authority._write(token, "outcome_reconciled", update).result

    def _validate(self, proposal: Proposal, pull: PullRequest) -> None:
        task = self.read_task(proposal.task_id)
        if task.get("id") != proposal.task_id or revision(task) != proposal.task_revision:
            self._conflict()
        if (task.get("pr_url"), pull.url, pull.head_sha, pull.base) != (
            proposal.pr_url,
            proposal.pr_url,
            proposal.head_sha,
            "dev",
        ) or not pull.identity:
            self._conflict()
        proof = proposal.proof
        if (
            not proof.get("artifact")
            or not proof.get("run")
            or proof.get("sha") != proposal.head_sha
            or self.verify_proof(proof) is not True
        ):
            raise SwarmError("required proof artifacts are absent or unverified")

    def _conflict(self) -> None:
        self.authority.store.redis.incr(self.authority.store.key(self.authority.slug, "outcome-conflicts"))
        raise SwarmError("outcome_conflict")

    def outcome_conflicts_total(self) -> int:
        store = self.authority.store
        return int(store.redis.get(store.key(self.authority.slug, "outcome-conflicts")) or 0)


class CompletionGate:
    def __init__(self, outcomes: Outcomes, token: str, generation: int, outcome: dict) -> None:
        self.outcomes, self.token, self.generation, self.outcome = outcomes, token, generation, outcome

    def apply(self, doc: dict, op: dict, ctx, apply_op) -> bool:
        from scripts.swarm_ledger import ledger_tasks

        claim = self.outcomes._receipt(self.token, self.generation, self.outcome)
        accepted = ledger_tasks.complete_outcome(doc, op, ctx, self.outcome, claim.holder)
        self.outcomes._receipt(self.token, self.generation, self.outcome)
        return accepted
