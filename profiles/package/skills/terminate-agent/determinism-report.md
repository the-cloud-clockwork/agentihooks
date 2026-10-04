# Terminate Agent Determinism Report

## Decomposition

1. Parse selector, harness filter, list mode, and dry-run mode.
2. Correlate the AgentiHooks registry with live process identities.
3. Resolve an exact name, UUID, or PID and reject ambiguity.
4. Validate PID identity, process-group boundaries, shared sessions, and caller isolation.
5. Preview without signals, or terminate with TERM, bounded wait, identity revalidation, KILL, and exit proof.

## Classification

| Step | Classification |
|---|---|
| Argument parsing | Deterministic |
| Session/process discovery | Deterministic |
| Exact target resolution | Deterministic |
| Safety validation | Deterministic |
| Dry-run preview | Deterministic |
| Signal escalation and verification | Deterministic |

Operator judgment is required only when duplicate names or a shared Codex host require choosing the intended UUID or blast radius.

## Scripts

| Primitive | Responsibility |
|---|---|
| `agentihooks terminate-agent` | Complete deterministic workflow implemented by `scripts.terminate_agent` |

## Rewritten Workflow

The skill invokes `agentihooks terminate-agent` for listing, dry-run validation, termination, escalation, and verification. It never derives process commands in prose.

## Token Estimate

The primitive removes repeated process discovery, PID/PGID correlation, safety reasoning, and signal-loop construction from each invocation. Estimated workflow-token reduction: 80–90%.
