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
  (`ScopeRefused`); an older attempt cannot overwrite a newer result.
- A session ID outside `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` is never stored.
- A partially written last line is skipped on read, and the next write starts on a fresh line.

Live sources: session start records the launch scope (resolved project, Git branch and the `AGENTIHOOKS_SWARM`,
`AGENTIHOOKS_SWARM_TASK` and `AGENTIHOOKS_SWARM_LANE` variables). At Stop, `observe_transcript` records a
transition at the time of each transcript entry whose working directory changed (Claude `cwd` and `gitBranch`,
Codex `turn_context`), so a worktree switch is placed where it happened. Task revision is filled only by callers
that know it; the live hooks leave it empty.

## Attribution

`scope_at(session_id, at)` returns the scope in force at an instant. `attribute(session_id, events, grant=None)`
labels each event:

| Label | Meaning |
|---|---|
| `scoped` | the transition in force names a project |
| `explicit` | the event names its own `project_id`, admitted by the grant; other fields come from the event time |
| `fleet` | the event says `share=fleet`; it carries no project fields |
| `unknown` | no transition was in force, or it named no project; a path or basename is never promoted to a project |
| `refused` | the event names a project outside the grant |

`unattributed_session_events_total(results)` counts `unknown` and `refused` results.

Brain markers take the time of the transcript record that holds them. A marker whose time falls under a
recorded transition gets that scope's fields and its `attribution`; explicit marker attributes win. A marker
marked `share=fleet` gets no project attributes. A marker with no recorded transition keeps the preceding
lookup by session.

## Grant

A `SessionGrant` lists the project IDs a session may claim. With a grant, a transition or explicit marker
project outside it is refused before anything is written, even when the folder exists locally. A scope with no
project (`""` or `unknown`) makes no claim and is admitted as unknown. Signed launch grants belong to
SV2-IDN-04; until then the live hooks pass no grant, and this package grants no authority from any label.

## Rollback

The scope log is authoritative history and is never rewritten to a default project. Rolling back the code
stops writing transitions and returns markers to the session lookup; `project-sessions.jsonl` and its
`lookup` are unchanged by this package, so earlier readers keep working.
