# Swarm v2 architecture decisions

Package SV2-FND-02, record revision 1. Generated from `docs/swarm-v2/architecture.json` by `python -m scripts.swarm_v2.architecture render`; edit the record, never this file.

## Components

| Component | Kind | Code owner | State owner | Deployment owner | Authoritative state |
|---|---|---|---|---|---|
| Swarm reconciliation controller | dispatcher | agentihooks | agentihooks | antoncore | Swarm state and ledger: task claims, generations and leases |
| Local runtime adapter | service | agentihooks | agentihooks | antoncore | Execution identity plus the observed herdr runtime on the controller host |
| Kubernetes runtime adapter | service | agentihooks | agentihooks | antoncore | Execution identity plus observed execution Pods |
| Worker image and entrypoint | service | agentihooks | agentihooks | antoncore | Immutable image and launch specification |
| Session archive and catalog service | service | agentihooks | agentihooks | antoncore | Durable transcript archive and catalog |
| Transcript ingestion backlog | backlog | agentihooks | agentihooks | antoncore | Per-source accepted offsets awaiting archive |
| Session chunk embedding backlog | backlog | agentihooks | agentihooks | antoncore | Changed transcript chunks awaiting embedding |
| Swarm brain context selection and graph retrieval | service | agentibrain-kernel | agentibrain-kernel | antoncore | Swarm brain knowledge and provenance |
| Personal brain | service | agentibrain-kernel | agentibrain-kernel | personal installation | Personal brain knowledge, separate from the swarm corpus |
| Swarm brain routing and consultation gates | service | agentihooks | agentihooks | antoncore | Session brain binding and verified context receipts |
| Cluster infrastructure | service | antoncore | antoncore | antoncore | Cluster, AMIs, autoscaling groups, taints, GitOps and secrets delivery |
| Operator interface | service | agentihooks | agentihooks | antoncore | Ledger projections and runtime observations |

## AD-01: Ownership of orchestration, source evidence, cognition and infrastructure

Status: accepted.

agentihooks owns orchestration and source evidence: swarm reconciliation, runtime adapters, the worker image, session capture and catalog, brain routing and consultation gates. agentibrain-kernel owns cognition: context selection, graph retrieval and synthesis. antoncore owns infrastructure and the deployment of every Anton-hosted component.

Why: One change has one authoritative implementation owner even when several repositories consume its contract (plan sections 0.1 and 1.1).

Rejected alternatives:

- A new standalone product housing the controller or the session archive: Code that belongs in agentihooks, agentibrain-kernel or antoncore stays there (plan section 0.1).
- Keep agentibridge as the dispatcher, agent registry or transcript service: agentibridge is deprecated; only its chunking, schema and search logic may be extracted into agentihooks (plan section 9.2).

## AD-02: Controller-owned execution Pods are a targeted convention exception

Status: accepted.

A distributed execution attempt is a controller-owned Pod with restartPolicy Never, created and recreated only by the swarm reconciliation controller. The exception to the antoncore StatefulSet convention covers these ephemeral attempts only; long-lived services, the controller included, keep the existing conventions.

Why: A Pod is an execution attempt, not the identity of a seat; an automatic restart must not create a second unrecorded conversation (plan sections 1.2, 2.2 and 4.1).

Rejected alternatives:

- One StatefulSet per coding agent: A seat outlives any Pod; recreation follows a classified failure in the controller, not a StatefulSet restart.
- A fixed worker pool as the scaling mechanism: Pending execution Pods drive the existing node autoscaler; a pool pins idle capacity (plan sections 4.1 and 7.4).

## AD-03: One main agent per Pod, with the local path kept

Status: accepted.

Each execution Pod hosts one main swarm agent under one process supervisor that owns the headless herdr server, the session exporter and the agent. Sub-agents are child processes inside the Pod's resource limits unless scheduled as separate swarm tasks. The local herdr runtime stays a supported compatibility path, and distributed mode stays off until its gate passes.

Why: INV-R11: a local swarm stays functional when distributed mode is disabled; resource budgets must cover every child process (plan sections 1.2 and 4.1).

Rejected alternatives:

- Bespoke per-node worker daemons hosting several agents: The baseline avoids per-node daemons; Kubernetes stays the placement scheduler (plan section 1.2).

## AD-04: Controller and session archive are separate processes from one package

Status: accepted.

The swarm controller and the session archive and catalog service may run as separate processes supplied by the agentihooks package, each deployed by antoncore. The archive uses the existing Postgres and pgvector infrastructure where its capacity and isolation have been verified. Workers reach both over authenticated HTTP.

Why: The session service may run as a process supplied by the agentihooks package beside the controller, and must not run a complete database stack inside every worker image (plan section 1.2).

Rejected alternatives:

- A complete database stack inside every worker image: Workers carry no durable store; the archive uses the existing Postgres and pgvector infrastructure where its capacity and isolation have been verified (plan section 1.2).

## AD-05: One coding-task authority; backlogs never dispatch work

Status: accepted.

The swarm reconciliation controller and its ledger are the only authority that claims and dispatches coding tasks. Bounded backlogs are permitted for transcripts and changed content; a backlog never launches an agent. Any other component that carries coding tasks or launches agents is rejected unless operator_changes names that proposal id with the digest of its exact content, the approving operator, the record revision it was approved at, a reason and an Ed25519 signature from the operator transport; only an approval that authenticated over that transport is signed, and the record is checked with the public key alone, and a label in the record or a field on the proposal never approves it. Classification is by declaration: every proposal declares what it carries, whether it launches agents and the authoritative state it owns, and the Spec reader checks the declaration against that state.

Why: Retain one swarm task model and one authority for task claims (plan section 1.2).

Rejected alternatives:

- KEDA for the baseline design: Pending execution Pods and the existing node autoscaler cover scaling (plan section 1.2).
- A second work-stealing queue for coding tasks: Two claim authorities break generation fencing (plan sections 1.2 and 4.3).
- The brain or session search as a task scheduler: Cognition and evidence retrieval inform tasks; they never dispatch them (plan section 0.1).

## AD-06: Workers never carry the brain stack

Status: accepted.

The worker image holds only the permitted components: process supervisor, headless herdr server, session exporter, agentihooks, Claude CLI, Codex CLI and the developer toolchain. It excludes the brain database, the brain tick stack, the brain model server and any transcript database. Any other proposed worker component is an unresolved decision until the operator adds it to worker_permitted. Workers reach the selected brain and the session archive over authenticated HTTP.

Why: One shared swarm brain runs on durable Anton infrastructure; no brain stack per worker or per coding task (plan sections 6.1 and 12.1). Section 6.1 lists what the image installs and section 4.1 names the supervisor, the headless herdr server, the session exporter and the main agent.

Rejected alternatives:

- A complete brain stack per worker or per coding task: Duplicates storage and inference cost per attempt and breaks brain isolation (plan section 12.1).

## AD-07: Swarm and personal brains are separate deployments

Status: accepted.

The swarm brain and the swarm brain routing and consultation gates are deployed by antoncore. An optional personal brain, with the routing and gates its personal sessions use, is a separate installation with its own deployment owner; it is not a second owner of the swarm brain and never joins the execution control plane.

Why: Plan section 1.1 names antoncore or a personal installation for brain components; splitting them gives each component one deployment owner. Section 0: personal development may use a separate personal brain without joining the execution control plane; section 12.3: a swarm launch cannot silently fall back to personal memory.

Rejected alternatives:

- One brain component deployed either by antoncore or by a personal installation: A component with two deployment owners has no single owner to answer for it.

## AD-08: Transcript evidence and brain synthesis keep separate storage domains

Status: accepted.

Transcript evidence and brain synthesis keep separate logical storage domains even when they share a database server. Redis keeps coordination state where it already exists and is never the only durable transcript archive. The initial release uses a relational edge table with bounded traversal instead of a graph database.

Why: Plan section 1.2 and INV-M01: canonical transcript storage must not be a truncated Redis preview.

Rejected alternatives:

- Redis as the only durable transcript archive: Redis previews are truncated and not a durable archive (INV-M01, plan section 1.2).
- A graph database in the initial release: A relational edge table and bounded traversal cover the proposed graph contract (plan section 1.2).

## Permitted worker image components

- process supervisor
- headless herdr server
- session exporter
- agentihooks
- Claude CLI
- Codex CLI
- developer toolchain

## Worker image exclusions

- brain database
- brain tick stack
- brain model server
- transcript database

Operator architecture changes: none.

## Unresolved decisions

None.

## Rejected proposals

None.
