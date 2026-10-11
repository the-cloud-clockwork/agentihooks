"""Pod creation joined to the runtime operation journal: one deterministic Pod per execution, adopted only on a match."""

from collections import Counter
from dataclasses import dataclass

from scripts.swarm_v2.kubernetes import adoption
from scripts.swarm_v2.kubernetes.client import AlreadyExists, ApiRefused, PodApi
from scripts.swarm_v2.kubernetes.spec import DOMAIN, PodSpecRefused, PodTemplate, Rendered
from scripts.swarm_v2.kubernetes.watch import BACKEND, EXECUTION_LABEL, OWNER_LABEL, owner_for
from scripts.swarm_v2.runtime.operations import SPAWN, Observation, Operation, Phase

GENERATION_LABEL = f"{DOMAIN}/generation"
SPEC_DIGEST = f"{DOMAIN}/spec-digest"
OPERATION_DIGEST = f"{DOMAIN}/operation-digest"
OUTCOMES = ("created", "adopted", "observed", "quarantined", "disabled")


@dataclass(frozen=True)
class PodStatus:
    """Runtime observation only: Succeeded means the container exited, never that the ledger outcome committed."""

    name: str
    uid: str
    phase: str
    reasons: tuple[str, ...]
    deleting: bool


def pod_status(pod: dict) -> PodStatus:
    metadata, status = pod["metadata"], pod.get("status", {})
    reasons = [status["reason"]] if status.get("reason") else []
    for container in status.get("containerStatuses", []):
        reasons += [state["reason"] for state in container["state"].values() if state.get("reason")]
    phase = status.get("phase", "Unknown")
    return PodStatus(metadata["name"], metadata["uid"], phase, tuple(reasons), "deletionTimestamp" in metadata)


class KubernetesTransport:
    backend = BACKEND

    def __init__(self, api: PodApi, slug: str, template: PodTemplate, creation_enabled: bool = True) -> None:
        self.api, self.template, self.owner = api, template, owner_for(slug)
        self.creation_enabled = creation_enabled
        self.reconciliations = Counter()
        self.quarantined: dict[str, dict] = {}
        self._prepared: tuple[dict, Rendered | PodSpecRefused] | None = None

    def observe_operation(self, operation: Operation) -> Observation:
        if operation.action != SPAWN:
            return Observation(Phase.REFUSED)
        try:
            pods = self.api.list_pods(self._selector(operation.execution_id))
        except ApiRefused:
            return Observation(Phase.UNKNOWN)
        if not pods:
            # Once a create was sent, a missing Pod may have run and been deleted, so it proves no absence.
            return Observation(Phase.UNKNOWN if operation.attempted else Phase.ABSENT)
        return self._judge(operation, pods, "observed", None)

    def apply_operation(self, operation: Operation, payload: dict) -> Observation:
        if operation.action != SPAWN:
            return Observation(Phase.REFUSED)
        if not self.creation_enabled:
            self.reconciliations["disabled"] += 1
            return Observation(Phase.ABSENT)
        body = self._body(operation, payload)
        if body is None:
            return Observation(Phase.REFUSED)
        try:
            return self._applied(self.api.create_pod(body), "created")
        except AlreadyExists:
            return self._adopt(operation, body)
        except ApiRefused:
            return Observation(Phase.REFUSED)

    def spec_digest(self, payload: dict) -> str:
        try:
            rendered = self.template.render(payload)
        except PodSpecRefused as refused:
            self._prepared = (payload, refused)
            return ""
        self._prepared = (payload, rendered)
        return adoption.identity(rendered.digest, rendered.pod["spec"])

    def status(self, execution_id: str) -> tuple[PodStatus, ...]:
        return tuple(pod_status(pod) for pod in self.api.list_pods(self._selector(execution_id)))

    def kubernetes_create_reconciliation_total(self) -> dict[str, int]:
        return {outcome: self.reconciliations[outcome] for outcome in OUTCOMES}

    def _selector(self, execution_id: str) -> str:
        return f"{OWNER_LABEL}={self.owner},{EXECUTION_LABEL}={execution_id}"

    def _render(self, payload: dict) -> Rendered:
        # The journal renders at prepare; apply reuses that render so one spawn counts one refusal.
        prepared, self._prepared = self._prepared, None
        if prepared is None or prepared[0] != payload:
            return self.template.render(payload)
        if isinstance(prepared[1], PodSpecRefused):
            raise prepared[1]
        return prepared[1]

    def _body(self, operation: Operation, payload: dict) -> dict | None:
        try:
            rendered = self._render(payload)
        except PodSpecRefused:
            return None
        metadata = rendered.pod["metadata"]
        expected = {
            OWNER_LABEL: self.owner,
            EXECUTION_LABEL: operation.execution_id,
            GENERATION_LABEL: str(operation.generation),
        }
        if (
            metadata["namespace"] != self.api.namespace
            or {key: metadata["labels"][key] for key in expected} != expected
            or adoption.identity(rendered.digest, rendered.pod["spec"]) != operation.spec_digest
        ):
            return None
        # The API server defaults the live spec, so a later match compares the digests and the adoption fields.
        metadata["annotations"].update({SPEC_DIGEST: rendered.digest, OPERATION_DIGEST: operation.payload_digest})
        return rendered.pod

    def _adopt(self, operation: Operation, body: dict) -> Observation:
        try:
            existing = self.api.read_pod(body["metadata"]["name"])
        except ApiRefused:
            return Observation(Phase.UNKNOWN)
        if existing is None:
            return Observation(Phase.UNKNOWN)
        return self._judge(operation, [existing], "adopted", body)

    def _judge(self, operation: Operation, pods: list[dict], outcome: str, rendered: dict | None) -> Observation:
        reasons = [self._mismatch(operation, pod, rendered) for pod in pods]
        if reasons == [""]:
            return self._applied(pods[0], outcome)
        for pod, reason in zip(pods, reasons):
            self._quarantine(pod, reason or "ambiguous")
        return Observation(Phase.REFUSED)

    def _mismatch(self, operation: Operation, pod: dict, rendered: dict | None) -> str:
        labels = pod["metadata"].get("labels", {})
        notes = pod["metadata"].get("annotations", {})
        if labels.get(OWNER_LABEL) != self.owner:
            return "owner"
        if labels.get(EXECUTION_LABEL) != operation.execution_id:
            return "execution"
        if labels.get(GENERATION_LABEL) != str(operation.generation):
            return "generation"
        if notes.get(OPERATION_DIGEST) != operation.payload_digest:
            return "operation"
        if rendered is not None and (reason := adoption.mismatch(rendered, pod)):
            return reason
        if adoption.identity(notes.get(SPEC_DIGEST), pod.get("spec", {})) != operation.spec_digest:
            return "spec"
        return ""

    def _applied(self, pod: dict, outcome: str) -> Observation:
        self.reconciliations[outcome] += 1
        metadata = pod["metadata"]
        return Observation(
            Phase.APPLIED,
            metadata["labels"][EXECUTION_LABEL],
            int(metadata["labels"][GENERATION_LABEL]),
            BACKEND,
            metadata["annotations"][OPERATION_DIGEST],
            {"uid": metadata["uid"], "name": metadata["name"], "namespace": metadata["namespace"]},
        )

    def _quarantine(self, pod: dict, reason: str) -> None:
        self.reconciliations["quarantined"] += 1
        metadata = pod["metadata"]
        self.quarantined[metadata["uid"]] = {"name": metadata["name"], "uid": metadata["uid"], "reason": reason}
