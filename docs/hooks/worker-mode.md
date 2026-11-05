---
title: Worker Mode
nav_order: 7
parent: Hook System
permalink: /docs/hooks/worker-mode/
---

# Worker Mode
{: .no_toc }

A headless worker run is a Claude Code or Codex session started unattended to
answer one input, with no operator and no swarm seat watching it. Its hooks run
in worker mode: every guardrail still applies, and the swarm and fleet
coordination text that a pane session receives is not injected.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

## The flag

| Variable | Value | Effect |
|---|---|---|
| `AGENTIHOOKS_WORKER_RUN` | `1` | Hooks run in worker mode |
| `AGENTIHOOKS_WORKER_RUN_ID` | run folder name | Names the run; set beside the flag, read by nothing in the hooks |

Only the exact value `1` turns worker mode on. `0`, `true`, an empty value or an
unset variable leave the session interactive. `hooks.context.worker_mode.active()`
is the one check every hook uses.

You do not set the flag by hand for a normal run. `agentihooks headless run`
sets both variables on the process it launches, and so does every workflow
launcher that runs a profile through the same profile runner. Other headless
calls, such as the read only blocker critic and the scout, do not set it.

## Usage

Run a profile once, unattended, and print its result as JSON:

```bash
agentihooks headless run documentation --input "Summarise docs/hooks/index.md"
```

The runner starts the harness through `agentihooks select-profile` with
`AGENTIHOOKS_WORKER_RUN=1` and `AGENTIHOOKS_WORKER_RUN_ID=<run folder name>` in
its environment, so every hook event of that session sees the flag.

| Option | Default | Meaning |
|---|---|---|
| `PROFILE` | required | Profile to render and run |
| `--input` | required | Prompt text, or a file holding it |
| `--schema` | none | JSON schema file the output must match |
| `--harness` | `auto` | `auto`, `claude` or `codex` |
| `--out` | `~/.agentihooks/headless/<UTC stamp>-<8 hex>` | Run folder for the transcript |
| `--tier` | `high` | Model tier, `high` or `low` |
| `--effort` | `medium` | Reasoning effort |
| `--timeout` | `1800` | Seconds before the run is killed |

The command prints one JSON line with `status` (`ok`, `failed`, `invalid` or
`timeout`), `output`, `session`, `exit_code`, `transcript` and `run_dir`, and
exits `0` only when `status` is `ok`.

To give a hand started session the same hook behavior, set the flag in its
environment:

```bash
AGENTIHOOKS_WORKER_RUN=1 claude -p "List the open TODOs in this repo"
```

## What worker mode turns off

| Hook output | Pane session | Worker run |
|---|---|---|
| Inbox items for the session's address | Shown at the next tool call, then confirmed delivered | Not shown and not confirmed; each item stays pending for its addressee's next interactive session |
| Local broadcasts at SessionStart, UserPromptSubmit, PreToolUse and PostToolUse | Injected by severity | Not injected |
| Broadcasts the brain adapter publishes on a feed refresh | Injected into that tool call | Not injected |
| Fleet broadcast claim from the swarm control API | Claimed into the local store each prompt | Not claimed, so the run consumes no fleet delivery |
| `SWARM REFOCUS` block for a session bound to a swarm task | On the first prompt, after compaction, on change and every `AGENTIHOOKS_REFOCUS_EVERY` calls | Never |
| Swarm ledger hook (operator events, the stop block for unhandled events) | Runs on session start, prompt, tool and stop events | Skipped; a worker run is never held at Stop for ledger events |
| `SWARM WAIT` nudge on a native `sleep` or `wait` in an `eng` or `ci` lane | Injected when no declared wait holds | Never |
| Claim watch (ends a lane worker whose task claim moved to another agent) | Checked at each tool call | Skipped |
| `LEDGER DECISION` directive after an accepted plan in a ledger bound session | Injected once per session | Never |

A worker run inherits `AGENTIHOOKS_SWARM`, `AGENTIHOOKS_SWARM_TASK` and the agent
name from the process that launched it. Without worker mode it would read its
parent's inbox, claim its parent's fleet broadcasts and receive its parent's
refocus block. Worker mode keeps those with the parent.

The session still registers in the broadcast store, so `agentihooks broadcast`
listings count it as a live session.

## What worker mode keeps

Every guardrail runs exactly as in a pane session, including:

- the secret scan on tool input, which blocks a write that carries a credential;
- the credential guard, which blocks reading `.env` and other credential files;
- the retry circuit breaker, which hard stops repeated identical failures;
- branch, production and kubectl mutation guards, enforcements and conditions;
- tool memory, the Bash output filter and the context preprocessor;
- the brain adapter's own session start injection.

A block in a worker run ends the tool call the same way it does in a pane: the
hook exits `2` and the harness reads the block reason.

## Verify

Check what a hook sees for a given environment:

```bash
AGENTIHOOKS_WORKER_RUN=1 python -c 'from hooks.context import worker_mode; print(worker_mode.active())'
```

`True` means the hooks of a session with that environment run in worker mode.

After a headless run, the transcript in the run folder holds no `SWARM REFOCUS`,
`LEDGER DECISION`, `SWARM WAIT` or broadcast blocks, and an inbox item sent to
the parent's address before the run is still pending:

```bash
grep -c "SWARM REFOCUS\|LEDGER DECISION\|SWARM WAIT" "$RUN_DIR/transcript.jsonl"
agentihooks msg inbox
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| A headless run shows the parent's inbox items or refocus block | The flag is not `1` in the harness process, for example `true` or a launcher that rebuilt the environment | Launch through `agentihooks headless run`, or export `AGENTIHOOKS_WORKER_RUN=1` exactly |
| An interactive pane gets no inbox items, broadcasts or refocus | `AGENTIHOOKS_WORKER_RUN=1` leaked into the pane's environment | Unset it and start a new session |
| A worker run is blocked on a tool call | A guardrail fired; worker mode does not lift guardrails | Read the block reason and change the call |
| An inbox item stays pending after a worker run read it | Worker runs never show or confirm inbox items | Read it from the interactive session that owns the address |

## Related

- [Broadcast System](broadcast.md)
- [Events](events.md)
- [Headless clamps](../reference/headless-clamp.md)
- [Swarm](../pillars/swarm.md)
