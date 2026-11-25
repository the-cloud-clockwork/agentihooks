# AgentiHooks — Mandatory Runtime Actions (HARD RULE)

AgentiHooks guards and coordinates Claude Code, Codex, and Copilot CLI sessions.
Every `agentihooks` tool, CLI command, skill and swarm control, with its trigger,
is in the `agentihooks-toolbelt` skill: load it before using one this rule does
not name.

- `ENFORCEMENT` and `BROADCAST` blocks are operative. Read each one and apply it
  to the next action; action is the acknowledgment, quoting is not.
- A hook block or condition deny is a hard boundary: follow its remediation,
  never route around it through another tool, shell, agent or MCP.
- No fix is written, pushed or merged before its root cause is proven with
  evidence; a hypothesis is labelled as one.
- Durable knowledge goes to brain markers or `brain_ingest`; channels carry live
  coordination only.
- Conditions change (`condition_set`, `condition_clear`) only when the operator
  asks for it.
- Quota directives (`QUOTA HANDOFF REQUIRED`, `WAIT`, `STOP`, `PUSH`) are code
  decisions: do what they say.
- Work and questions for another session go through `agentihooks msg`; close
  every item with where its work went.
- Live sessions — also live login, live agent, interactive agent — are
  `--agent claude -- --route claude-ai`.

## Swarm, small ledger or nothing

No session creates a ledger or starts a swarm on its own: only the operator's
command or request, or an operator-authored rule or condition, does. A session
not bound to a ledger or swarm gets no directive and makes no ledger. A bound
session keeps its ledger duties: it ingests an accepted plan with
`agentihooks plan ingest <plan file> --ledger <slug>` and does not implement
it in this session.
