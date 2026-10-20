# Fenced restore decisions

Package SV2-CKP-05. When a worker Pod fails, the Kubernetes controller fences the lost attempt, decides how its
seat returns and launches exactly one replacement. The replacement restores from a verified checkpoint before the
agent starts and reports which kind of continuity it got. The ledger, `swarm status` and the swarm panel show that
kind; a restore never reports more continuity than it delivered.

| Continuity | Meaning | When it happens |
|---|---|---|
| `native` | The harness reopens the same conversation from its own captured session files | The checkpoint carries a qualified native session, its session id matches the one the fenced attempt held, and the harness version on the replacement is supported |
| `reconstructed` | A new conversation starts with the archived transcript as its opening context | A complete context only checkpoint, chosen by the master or operator |
| `fresh` | A new conversation with no restored state | Chosen by the master or operator, or a superseded legacy spawn (see [Limitations](#limitations)) |

Checkpoints themselves are described by the checkpoint schema in `docs/swarm-v2/schemas/checkpoint.json`; where
checkpoint storage may live is in [Swarm v2 storage policy](storage-policy.md).

## What happens when a Pod fails

The controller's recovery pass (`Recovery.reconcile`) lists the swarm's worker Pods every pass and classifies each
current attempt. A failure reason is one of `oom_killed`, `evicted`, `node_lost`, `image_pull` (after a two minute
grace) or `application_exit`.

1. **Fence.** The controller writes one fence record for the attempt: its execution id, generation, seat, reason,
   controller epoch, the native session id the attempt held, and the archive watermark, the last transcript record
   known to be archived. The record also names the tail that is lost: transcript written after the watermark and
   not archived before the failure. Only the current attempt of a seat can be fenced; a second fence for the same
   attempt returns the first.
2. **Release.** The fenced attempt's launch grant is revoked and its provider account slot ends. Each release is
   recorded in the fence, so a controller restart resumes where it stopped and never releases twice. A fenced worker
   keeps no claim, grant or account authority.
3. **Decide.** The controller resumes natively without asking when all of these hold: restore automation is on and
   not paused, the reason is not `image_pull`, the attempt held a native session id, and the newest complete
   checkpoint compatible with the current worker image is a native one. Otherwise the seat's attempt moves to
   `awaiting-decision` and the decision stays `recovery_pending` until the master or operator chooses.
4. **Replace.** A decision to resume or start fresh marks the old attempt `fenced`, admits one replacement execution
   on the same seat with the next generation, and writes the restore directive the replacement launches with.
5. **Hold the task.** While a fenced attempt has no launched replacement, or the replacement has not reported its
   outcome, the task's `awaiting` field reads `recovery_pending` on the ledger (see
   [awaiting values](../pillars/swarm.md)). Once the outcome arrives the controller writes the prior value back and
   stores the outcome on the task's `recovery` field.

Every fence and decision is recorded through the controller's telemetry as a `fence` or `recovery` event with the
expected and observed generation and the controller epoch, and every write runs under the controller's lease, so a
controller that lost its lease changes nothing.

## Choosing resume or fresh

A seat awaiting a decision shows in the Handoff outcomes panel of the ledger page with two controls. The card's
reason names the failure and, when a verified checkpoint exists, how it would continue and from which record, for
example `oom_killed; native resume from ckpt-7 at record 412`. The resume control reads **Resume native** when the
checkpoint supports native resume and the attempt held a session, and **Continue from context** otherwise. The same
choice is available from the command line:

```bash
agentihooks swarm example restore-decision eng-2 resume
agentihooks swarm example restore-decision eng-2 fresh
```

For a Kubernetes attempt the command only records the request; the controller applies it under its lease on its next
recovery pass and then drops the request. A refused request is logged by the controller as
`controller: the recovery request for EXECUTION was refused: REASON`.

Callers with a controller credential can post the choice directly:

```bash
curl -sS -X POST "$CONTROL_URL/v1/executions/$EXECUTION/restore-decision" \
  -H "Authorization: Bearer $OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"choice": "resume", "agent": "master@example-1"}'
```

Only the operator, or a registered master of the swarm whose credential is bound to the named agent, can choose. Any
other caller is refused with `forbidden_scope`; the choice is never inferred from a display name.

- `resume` picks the newest complete, compatible checkpoint of the fenced attempt. Native continuity is chosen when
  that checkpoint is native and the attempt held a session id; otherwise the replacement is reconstructed from
  context. With no usable checkpoint the request is refused with `no complete compatible checkpoint to resume from`
  and the seat keeps waiting.
- `fresh` admits a replacement with no checkpoint. The replacement's task row is read from the ledger so it starts
  from the task's current text.

### Repeated and conflicting requests

A decision is accepted once per fenced attempt. Repeating the accepted choice, including after a controller restart,
returns the same decision, the same checkpoint and the same replacement, and admits nothing new. A different choice
after a decision was accepted is refused with `no recovery decision is pending for this attempt`, and the accepted
decision, fence, directive and ledger row stay as they were. Requests are deduplicated by attempt and choice; they
carry no request id of their own.

## How the replacement restores

The replacement's launch record carries a `restore` directive with four fields: `checkpoint_id`, `recovery`
(`native`, `reconstructed` or `fresh`), `session_id` (set only for native) and `watermark`. A directive whose fields
disagree, for example a fresh directive naming a checkpoint, is refused as malformed.

The worker entry point runs the restore after its checkout is prepared and before the supervisor starts the harness,
so a natively resumed worker also has its task worktree:

1. The checkpoint is verified through the store granted to this attempt (below). A checkpoint that does not verify
   stops the launch.
2. The checkpoint's project must be one of the projects in the worker's registration.
3. For native continuity, the checkpoint's `native_session_id` must equal the directive's `session_id`, and the
   checkpoint must be a native one. The native session files are restored into the attempt's harness home and the
   harness version is checked. Native state lands before source state, so a version or identity refusal leaves the
   work tree untouched.
4. Committed and uncommitted source captured in the checkpoint is applied to the worktree, cloning the project first
   when the worktree has no repository.
5. For reconstructed continuity, the transcript is written to `continuation.txt` in the attempt folder, readable only
   by the worker user, and passed to the harness as its opening prompt after the line
   `Continue from this recorded conversation. Native resume was unavailable.`
6. For native continuity, the supervisor runs the harness's native resume command instead of the original command.

A native resume reads conversation records split on newlines only, so a conversation that holds a raw Unicode
paragraph or line separator restores instead of being refused as a changed format.

A worker launched with `checkpoint_remote: true` then reports the outcome to the controller and logs one summary
line, with no credentials and no conversation content:

```json
{"event": "restore_verified", "execution_id": "e-42", "recovery": "native", "checkpoint_id": "ckpt-7", "watermark": 412}
```

The controller accepts an outcome only from the replacement's own worker credential, only while that replacement is
still the seat's current attempt, and only when its continuity type, checkpoint and session id equal the accepted
directive. The stored outcome is marked `verified` and keeps the fence's `archive_watermark` beside the restored
watermark. A second identical report returns the stored outcome; a different one is refused.

### Refusals

A restore that cannot deliver its directive stops the launch with a reason. It never falls back on its own to a
weaker kind of continuity and never reports a fresh conversation as resumed: the fenced attempt's claim and the
accepted decision are left in place for the master or operator.

| Reason | Cause |
|---|---|
| `manifest_absent`, `corrupt_manifest` | The checkpoint record is missing or does not match its stored hash |
| `missing_object`, `corrupt_object` | An artifact the record lists is missing or its content no longer matches its hash |
| `unsupported_version`, `invalid_record` | The record does not satisfy a supported checkpoint schema version |
| `not_committed`, `missing_material`, `missing_watermark`, `inventory_mismatch` | The record was never committed, lacks a required artifact role or durable transcript cursor, or its hashes disagree with its inventory |
| `scope_mismatch`, `identity_mismatch` | The record belongs to another task, a newer task generation, a different execution generation, or names another checkpoint id |
| `context_only` | A native directive names a checkpoint that supports only context continuation |
| `conversation_mismatch` | The checkpoint's native session is not the one the fenced attempt held |
| `unqualified_version`, `version_changed` | The replacement's harness version is not qualified for native resume, or differs from the captured one |
| `source: …` | The captured source could not be applied to the worktree |
| `no checkpoint store is configured for this worker` | The directive names a checkpoint but the worker has no checkpoint store |

### Checkpoint validation and generations

`Validator.verify` reads every object through the attempt's granted artifact store and returns a verdict that names
no record values or paths. A checkpoint is authoritative only when its record and every artifact it lists are present
and match their hashes. The worker capture path and the replacement attempt both call it.

A checkpoint record carries two generations in its `authority`: `task_generation`, the task claim generation, and
`execution_generation`, the attempt's own generation. The validator checks each against its own source: the task
generation against the store's scope, which also scopes remote storage paths, and the execution generation against
the attempt's registration. Records written before `execution_generation` existed still validate, by reading the
task generation in its place.

## Configuration

| Setting | Where | Effect |
|---|---|---|
| `AGENTIHOOKS_CHECKPOINT_BUCKET` | Controller environment | Lists and fetches checkpoints from the object storage bucket; takes precedence over a root folder |
| `AGENTIHOOKS_CHECKPOINT_ROOT` | Controller or worker environment | Absolute path of a local checkpoint folder; a relative path is refused |
| `checkpoint_remote: true` | Worker launch record | The worker reads and writes checkpoints through the controller's scoped upload route and reports its restore outcome; the worker sets `AGENTIHOOKS_CHECKPOINT_REMOTE=true` for itself |

With no checkpoint store configured, the controller lists no checkpoints, so every failure waits for a decision and
only `fresh` succeeds.

Native resume is qualified for Claude Code 2.1 and Codex 0.162. Checkpoints are offered only when they were captured
under the same compatibility key as the controller's current worker image tag.

Worker bootstrap marks Claude Code's first run onboarding as completed before the harness starts, so a Pod worker
reaches its first turn and writes a native transcript that a later checkpoint can capture.

## Observing recovery

- **Ledger page.** The task row shows `RECOVERY PENDING` while the replacement is pending, and the seat's card in
  Handoff outcomes shows the failure, the offered checkpoint and, once reported, the continuity type.
- **Status.** `agentihooks swarm example status --json` lists the same cards under `handoffs`, each with
  `checkpoint`, `resume_available`, `resume_label` and `continuity` (`pending` until the replacement reports).
- **Task record.** The task's `recovery` field holds the accepted directive with status `accepted`, then the verified
  outcome.
- **Metrics.** `Recovery.restore_outcomes_by_type` counts verified outcomes as `native`, `reconstructed` and
  `fresh`. On the worker, `Restorer.metrics` counts outcomes and refusals under `restore_outcomes_by_type`, with
  refusals as `refused/<reason>`.
- **Worker log.** `restore_verified` lines, as above.

## Rollback

Recovery keeps a pause switch, stored as the swarm's `recovery-paused` key in Redis and set through
`Recovery.pause`. While paused, every failure is fenced and released as usual but waits for an explicit decision; no
replacement launches on its own. Checkpoints, fences and awaiting decision states are kept, and the pause survives a
controller restart. No command line or page control sets the switch yet. Checkpoint objects are authoritative data:
never delete them as caches during a rollback.

## Limitations

- A spawn superseded as a legacy specification is replaced fresh without a choice while automation is on and not
  paused, because it never started and holds no conversation. With automation off or paused it waits like any other
  failure. This is an approved exception to the rule that only the master or operator chooses fresh.
- Transcript written after the archive watermark is lost and its length is unknown; the fence names it.
- The live capture and restore proof on Anton is pending. The evidence record `evidence/SV2-CKP-05/result.json`
  is marked mocked and lists the fixture inputs: a valid checkpoint, a corrupt object, an unsupported harness
  version, a conversation mismatch and a duplicated restore request.

## Troubleshooting

- **The card offers only fresh.** No complete checkpoint compatible with the current worker image exists for the
  attempt, or the controller has no checkpoint store configured. Check the controller's checkpoint settings and
  whether the worker image tag changed since the checkpoint was captured.
- **The card reads Continue from context instead of Resume native.** The newest usable checkpoint is context only,
  or the attempt never recorded a native session id.
- **Resume was chosen but the worker did not start.** The worker refused the restore; its log names the reason from
  the table above. The decision stays accepted, so choosing again returns the same decision. Fix the cause, for
  example the worker image's harness version, and relaunch.
- **A choice was refused as no longer pending.** A decision was already accepted for that attempt, either
  automatically or by an earlier request. Read the card or the task's `recovery` field for the accepted type.
- **A task stays RECOVERY PENDING.** See the troubleshooting list under awaiting values in the
  [swarm pillar](../pillars/swarm.md).
