---
title: Operator presence
parent: Reference
nav_order: 79
permalink: /docs/reference/operator-presence/
---

# Operator presence

Every swarm agent starts with the operator marked absent from its pane:
**operator off**. This covers agents on a hive and on the workstation,
engineers, CI agents and masters alike. While off, the agent's chat replies
are held to twenty words in machine style, the question tool is refused and
permission prompts do not open. The operator lifts this only by typing
`operator on` in that agent's own pane. A session outside a swarm is never
gated.

The rule exists because swarm agents run unsupervised: the operator does not
read their panes, so an agent must never write as if he did. Anything an agent
needs decided goes to its ledger's front door master by inbox.

The hooks enforce it in code. The reply limit is checked at the Stop hook, so
an over limit reply is refused and the agent must write it again; an injected
hint alone is not relied on.

## Which sessions are gated

A session is gated when any of these holds:

| Binding | How it is set |
|---|---|
| `AGENTIHOOKS_SWARM` is set in the session environment | The tick launches every swarm agent with it, on any hive or the workstation |
| A ledger session binding exists for the session id | A successful `agentihooks ledger --slug <slug> --as <name> join` writes it under the ledger folder's `.sessions` directory (`LEDGER_DIR`, default `~/development-ledger`), so a session that joins a ledger is gated even outside a swarm |
| A master seat record exists for the session id | The `take-master` skill records it under `master_seats` in the AgentiHooks home, so a master opened by hand is gated without swarm environment |

A session with none of these is standalone: it is always treated as present,
its replies have no word limit and its question tool and permission prompts
work normally. Where the agent runs (a hive id, the workstation) plays no part
in the decision.

## Switching it on and off

| Event | Result |
|---|---|
| Session start or restart | Off. A start after a context compaction keeps the current state |
| The operator types `operator on` in the pane | On. The agent receives `Operator on: the operator is present in this pane until he types operator off or the session restarts.` |
| The operator types `operator off` in the pane | Off. The agent receives `Operator off: the operator is not present in this pane.` followed by the away reminder |
| Any other prompt the operator types while off | Stays off. A typed prompt is not a presence signal on its own |

The phrase matches as whole words, ignoring case and extra spaces, anywhere in
the typed prompt. When a prompt holds both phrases, `operator off` wins.

Only the operator's own typed words switch the state. Inbox items, cross
session messages, tick wake prompts and the launch prompt never do, even when
their text contains `operator on`.

### Master quiet window

A master seat has one extra presence rule. When the operator types any prompt
in a master pane, the master counts as present for the inbox quiet window,
`AGENTIHOOKS_INBOX_QUIET_S` seconds (default 180). During that window the
master's reply to him has no word limit and it receives
`Operator present in this pane during the quiet window. Answer his message.`
The question tool stays refused in that window; only `operator on` opens it.
Engineers and CI agents have no quiet window.

## What operator off enforces

### The reply limit

When an agent ends its turn, the Stop hook counts the words of its final
message. Over the limit, the hook blocks the stop with:

```text
Operator not present: this reply has 34 words, over the 20 word limit. Reply again in at most 20 words, machine style: result only, no explanation.
```

The agent then writes a shorter reply. Counting rules:

- Words are runs of text separated by whitespace.
- A paired brain marker, such as `<!-- @lesson -->` through `<!-- @/lesson -->`,
  is removed with everything inside it, so markers never count against the
  limit.
- Any other closed HTML comment is removed; the text after it counts.
- An unclosed `<!--` counts as ordinary text.

The hook refuses at most three times in a row. A fourth consecutive over limit
reply in the same stop sequence ends the turn, so an agent can never loop on
the Stop hook. The count starts again at the next turn.

A Stop condition that blocks takes precedence over the reply limit. If the
reply check itself fails, the hook logs `conditions stop failed` and the stop
proceeds.

### The away reminder

While off, the agent receives this reminder with every prompt it is given and
on every second tool call, uncompressed and kept out of the hook log:

```text
Operator not present: he never reads this pane. End the turn with no report, summary or ask addressed to him and skip the response template; any pane text stays within 20 words. Never write to him: send anything he must decide to the front door master by inbox with agentihooks msg send.
```

A master gets the same text ending `Never write to him first; answer only what
he writes.` instead of the inbox route.

### The question tool and permission prompts

| Agent | Question tool while off |
|---|---|
| Engineer or CI agent | Refused before it opens, with an instruction to send the question to `master@<slug>` by `agentihooks msg send` and keep working |
| Master | Refused, with an instruction to hold the question until the operator writes. When he has not typed in the master pane for ten minutes, the questions move to his Priorities as ledger questions instead (see the swarm pillar's event routing table) |

Permission prompts for `AskUserQuestion` and `ExitPlanMode` are denied with
`Denied <tool>: the operator is not present in this pane, and only his typed operator on opens this prompt. Keep working without it.`

## Configuration

| Setting | Default | Effect |
|---|---|---|
| `AGENTIHOOKS_OPERATOR_OFF_MAX_WORDS` | `20` | Word limit for replies and the number named in the reminder and refusal. Must be an integer |
| `AGENTIHOOKS_INBOX_QUIET_S` | `180` | Seconds a master counts as present after the operator types in its pane |
| `AGENTIHOOKS_SWARM` | unset | Gates the session when set |
| `LEDGER_DIR` | `~/development-ledger` | Where ledger session bindings are read |

Example: allow forty word replies for every agent a hive launches, by setting
the variable in the environment the agents inherit:

```bash
export AGENTIHOOKS_OPERATOR_OFF_MAX_WORDS=40
```

## Usage

Talk to one agent directly, then hand it back:

```text
operator on
explain why the mutation shard failed
operator off
```

Check a session's state. The state file lives under `operator_mode` in the
AgentiHooks home and is named by session id:

```bash
cat ~/.agentihooks/operator_mode/<session id>.json
```

`"on": true` means present. `"on": false` or a missing file means off for a
gated session. `refusals` counts consecutive reply refusals in the current
stop sequence.

## Limits

- Presence is per session. `operator on` in one pane never affects another
  agent, and a restart turns it off again.
- The limit applies only to the final message of a turn. Text written between
  tool calls is governed by the reminder, not refused.
- After three refusals in a row, an over limit reply is let through rather than
  trapping the agent.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| A session outside a swarm gets the reminder | It joined a ledger, took a master seat, or has `AGENTIHOOKS_SWARM` in its environment. A binding lasts for the session id; `ledger leave` does not remove it | Type `operator on` in that pane, or start a new session that does not join a ledger |
| `operator on` had no effect | It arrived through an inbox item, a relayed message or the launch prompt | Type it in that agent's own pane |
| A master answers the operator at length, then goes terse | The quiet window ended | Type `operator on` for a longer exchange |
| A swarm agent writes long replies | Its hooks are not installed, or `AGENTIHOOKS_OPERATOR_OFF_MAX_WORDS` was raised | Check the profile install and the session environment |
| The Stop hook keeps refusing | The reply still has more words than the limit after markers and comments are removed | Shorten the reply; the fourth consecutive attempt passes regardless |
