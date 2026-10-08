# Event-time session scope, version 2.0

SV2-IDN-03 records what a session was doing at each point, so its history is attributed to the task,
worktree and project in force when each event happened rather than to the session's latest working directory.

## Transitions

`hooks.context.project_sessions.record_scope(session_id, scope, at, grant=None)` appends one ordered
metadata event to the session's scope log under the agentihooks home (`brain/session-scopes/<session>.jsonl`).
A transition carries `project_id`, `project`, `repo`, `worktree`, `cwd`, `remote`, `branch`, `swarm`, `task`,
`task_revision` and `lane`, plus `sequence`, `at` and a deterministic `transition_id` (SHA-256 of the session,
time and scope). The schema is `urn:swarm-v2:session-scope`.

- A transition is written only when the scope differs from the latest accepted one.
- A replay with the same identity is a no-op, so replaying metadata after a restart adds no duplicate entry.
- A transition older than the latest accepted one, or one without an ISO 8601 time, is refused
  (`ScopeRefused`). For the same swarm and task, a numeric `task_revision` lower than the accepted one is
  refused too, so an older attempt cannot overwrite a newer result.
- A session ID outside `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` is never stored.
- A partially written or undecodable line is skipped on read, and the next write starts on a fresh line.

Live sources:

- Session start records the launch scope: resolved project, current Git branch, and the `AGENTIHOOKS_SWARM`,
  `AGENTIHOOKS_SWARM_TASK` and `AGENTIHOOKS_SWARM_LANE` variables.
- At Stop, `observe_transcript` records a transition at the time of each transcript entry whose working directory
  or branch changed (Claude `cwd` and `gitBranch`, Codex `turn_context` `cwd`). The branch comes only from the
  transcript, never from today's checkout. Codex entries carry no branch: an entry in the same folder keeps the
  branch already recorded, and one in a new folder records an empty branch.
- A failure to read or write the scope log never stops session start or Stop.

## Attribution

`scope_at(session_id, at)` returns the scope in force at an instant. `attribute(session_id, events, grant=None)`
labels each event:

| Label | Meaning |
|---|---|
| `scoped` | the transition in force names a project |
| `explicit` | the event names its own `project_id`, admitted by the grant; it replaces `project_id`, `project`, `repo` and `remote`, and every other field comes from the event time |
| `fleet` | the event says `share=fleet`; it carries no scope fields |
| `unknown` | no transition was in force, or it named no project; a path or basename is never promoted to a project |
| `refused` | the event names a project outside the grant; it carries no scope fields |

`unattributed_session_events_total(results)` counts `unknown` and `refused` results; Stop reports it on the
`brain.marker_write` span.

Brain markers take the time of the transcript record that holds them; a marker read from the Stop payload's last
message takes the Stop time. Once a session has a scope log, every marker is attributed through it:

- every scope attribute the model wrote is dropped, then the body takes the scope in force, plus `attribution`;
- a valid `project_id` claim keeps the model's `project_id`, `project`, `repo` and `remote` as written, without
  checking them against each other; task, worktree, branch, lane and revision still come from the event time;
- a `share=fleet` marker loses every scope field;
- a marker before the first transition, or a marker read from a transcript record without a time, is `unknown`
  and carries no project.

An outbox replay carries attributes computed when it was written. One that already carries an `attribution` is sent
as written; an older replay without one keeps the preceding lookup by session, which only fills missing attributes.
A session with no scope log keeps that lookup too.

## Grant

A `SessionGrant` lists the project IDs a session may claim. With a grant, a transition or explicit marker project
outside it is refused before anything is written, even when the folder exists locally. A scope with no project
(`""` or `unknown`) makes no claim and is admitted as unknown.

## Limitations

- SV2-IDN-04 supplies the grant as `Registration.session_grant()` (`launch-grant.md`). No live launch path
  registers a grant yet, so the live hooks pass none and an explicit marker `project_id` is accepted as
  written; this package grants no authority from any label.
- The live hooks see a task change only when a session starts under new swarm variables; a mid-session task
  change and `task_revision` are recorded by callers of `record_scope`.
- When a session resumes before its last Stop ran, transcript entries older than the resume are not recorded,
  and their markers are `unknown`.
- The marker idempotency key is unchanged (session, type and content), so identical marker text written under
  two tasks of one session still deduplicates to the first.
- Scope logs are kept like `project-sessions.jsonl`, with no retention sweep.

## Rollback

`AGENTIHOOKS_SESSION_SCOPE=0` disables the new path: no transitions are written and markers use the preceding
lookup by session, with the preceding body. The scope log is authoritative history and is never rewritten to a
default project; reverting the code leaves it in place, and `project-sessions.jsonl` and `lookup` are unchanged.
