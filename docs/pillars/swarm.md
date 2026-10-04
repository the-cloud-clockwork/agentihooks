---
title: "Swarm"
parent: The Four Pillars
nav_order: 8
permalink: /docs/pillars/swarm/
---

# Swarm
{: .no_toc }

Run a crew of Claude and Codex agents over the tasks of a swarm ledger, each in its own herdr pane.

1. TOC
{:toc}

---

## What a swarm is

A swarm is a herdr workspace named `swarm-<id>`. Its agents work the tasks of one swarm ledger
(`agentihooks ledger`), and every task belongs to a lane:

| Lane | Agent |
|---|---|
| `eng` | An engineer working a code task end to end with the dev-cycle skill. |
| `ci` | A CI engineer whose only job is CI speed; it adds each further bottleneck as a new `ci` task. |

The session that starts the swarm stays out of the work and speaks for the operator.

## Commands

The swarm id is a lowercase slug of letters, digits and dashes, starting with a letter, at most 48 long.

| Command | Effect |
|---|---|
| `agentihooks swarm list` | One line per swarm: state, caps, agent count, repo. |
| `agentihooks swarm tick` | One reconcile pass over every swarm (the timer runs it). |
| `agentihooks swarm <id> create --repo DIR [--max-eng-agents N] [--max-ci-agents N]` | Register a swarm, paused. Defaults: 2 eng, 1 ci. |
| `agentihooks swarm <id> start` | Run: enable the timer and scale up at once. |
| `agentihooks swarm <id> pause` | Stop new spawns; running agents continue. |
| `agentihooks swarm <id> stop` | Drain: no new spawns, the swarm stops when its agents finish. |
| `agentihooks swarm <id> stop --now` | Kill every agent and reopen its unfinished task. |
| `agentihooks swarm <id> status [--json]` | Config, task counts, and one row per agent. |
| `agentihooks swarm <id> set max-eng-agents=N max-ci-agents=N` | Change the caps; `swarm <id> max-eng-agents=N` also works. |
| `agentihooks swarm <id> set compact-limit=N` | Launch this swarm's next agents with `AGENTIHOOKS_COMPACT_LIMIT=N` (thousands of tokens); 0 keeps the default. |
| `agentihooks swarm <id> send-message TEXT` | Operator message to the swarm chat. |

A swarm is in one of five states: `running`, `paused`, `stopping`, `stopped`, `drained`. It drains when no
task is left to start and returns to `running` when a new task opens. Lowering a cap never kills work; the
count falls as agents finish.

Agent commands take the agent name from `--as` or `AGENTIHOOKS_AGENT_NAME`:

| Command | Effect |
|---|---|
| `agentihooks swarm <id> issue URL` | Record the task's GitHub issue. |
| `agentihooks swarm <id> pr URL` | Record the task's pull request; the task moves to `pr`. |
| `agentihooks swarm <id> done [--pr URL]` | Close the task; the swarm then closes the session. |
| `agentihooks swarm <id> block NOTE` | Comment the blocker, mark the task `blocked`, end the session. |
| `agentihooks swarm <id> handoff DOC` | Finish the session but keep the task: the next tick spawns a successor with the document in its prompt. A hook asks for it when the session reaches `AGENTIHOOKS_COMPACT_LIMIT` thousand tokens (default 600). |
| `agentihooks swarm <id> say TEXT [--to NAME\|eng\|ci]` | Post to the swarm chat. |

## One task per agent life

An agent is spawned for one task, told that task in its opening prompt, and ends when the task is done or
blocked. The prompt walks it through a fixed order: open an issue, create a worktree, red test then green,
pull request into `dev`, merge on green, then `done`. An agent that cannot finish pushes a draft pull request
and calls `block`. A finished agent is retired on the next tick and its pane closed.

## The minute tick

A systemd user timer (`agentihooks-swarm.timer`) runs `agentihooks swarm tick` every minute; nothing runs
between ticks. `start` installs and enables it. Each tick, per swarm:

1. Retire agents that finished.
2. Free the tasks of agents whose pane is gone, or that stayed idle for 10 ticks. An idle agent is nudged at 3.
3. Reopen claimed tasks that have no agent.
4. While `running`, spawn agents up to the caps, one per claimable task, as long as a Claude account has room
   under its session cap.
5. Mark the swarm `stopped` or `drained` when no agent is left and nothing remains to do.
6. Relay operator chat and deliver queued messages.

A lock keeps two ticks from running at once.

## Redis

Redis holds the swarm's runtime state, and the swarm refuses to run without it:

- the config of each swarm and the index of swarm ids;
- one claim per task, with a 10 minute lease that each tick renews for live agents, so a task has one owner;
- the agent registry;
- the chat outbox and the tick and flush locks.

The default is `redis://127.0.0.1:6379/0`; set `AGENTIHOOKS_SWARM_REDIS_URL` to use another. Task content
(title, lane, state, links) lives in the ledger, not in Redis.

## Talking to the swarm

- Operator: the chat on the ledger page, or `agentihooks swarm <id> send-message "@eng <text>"`.
- Agents: `agentihooks swarm <id> say "<text>"`, optionally `--to <agent name>`, `eng` or `ci`.

A message reaches an idle agent's pane at once. A busy agent's message waits in the Redis outbox, and every
tick delivers it once the agent is idle. Undelivered messages expire after 30 minutes.
