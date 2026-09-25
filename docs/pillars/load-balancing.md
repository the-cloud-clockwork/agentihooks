---
title: "Claude Account Load Balancing"
parent: The Four Pillars
nav_order: 7
---

# Claude Account Load Balancing
{: .no_toc }

Spread Claude Code sessions across several subscriptions, and decide by rule what
a session does when its own quota runs out.

1. TOC
{:toc}

---

## Accounts

Each subscription is one environment variable holding its OAuth token:

```bash
export AH_CC_TOKEN_work=...      # account "work"
export AH_CC_TOKEN_personal=...  # account "personal"
```

`agenti` (alias of `agentihooks claude`) and `agentihooks claude-terminal` launch
Claude on one of them. The launched session keeps only its own
`AH_CC_TOKEN_<slug>`; that variable name is how every tool below tells which
account a session runs on. Token values are never printed or logged.

## Launch routing

`agenti` probes every account (results cached 60 s in
`~/.agentihooks/claude-router-cache.json`) and picks one:

1. Drop accounts with less than 5% **routing left**, where routing left is
   `100 − max(5h used, 7d used)`: the tighter of the two windows.
2. Drop accounts already running `AGENTIHOOKS_MAX_SESSIONS_PER_ACCOUNT` live
   sessions (default 2), as long as another account is still below the cap.
3. Take the account with the most routing left.

When every routable account is at the cap, the one with the fewest live sessions
takes the new session and the launch line says `placement=overflow`. A lock held
until Claude starts keeps two simultaneous launches from both taking the last
free slot.

`agenti --route <slug>` skips all of this and uses `AH_CC_TOKEN_<slug>`.

```text
[agenti] account=work routing_left=62% 5h_left=80% 7d_left=62% sessions=1/2 source=cached
```

## Counting sessions

Live sessions are counted from `/proc`: every interactive `claude` process (not
`claude -p`) is attributed to the one `AH_CC_TOKEN_<slug>` name in its
environment, or to `unrouted` when it has none. A session that handed its work
off (below) no longer holds a slot.

```text
$ agentihooks balance
#  ACCOUNT   STATE   SESSIONS  ROUTING LEFT  5H LEFT  5H RESET  7D LEFT  7D RESET
1  personal  NORMAL  1/2       99%           99%      1h20m     99%      5d23h
2  work      NORMAL  2/2       54%           81%      30m       54%      5d10h
```

`agentihooks balance --current` marks the account this session runs on. The
session registry (`~/.agentihooks/active-sessions.json`) also records each
session's real Claude PID and account at SessionStart.

## Quota policy

Every tool call and prompt, the hook reads the session's own quota (the numbers
Claude Code reports to the status line) and, once a limit is hit, compares it
with every other account from the router cache. The result is one directive,
computed by `hooks/context/quota_policy.py`; nothing is left to the model's
judgment.

| This session | Other accounts | Directive |
|---|---|---|
| 7d used ≥ 98% | one with ≥ 20% routing left | `QUOTA HANDOFF REQUIRED` to it (accounts below the session cap first) |
| 7d used ≥ 98% | only accounts with 5–20% left | `QUOTA HANDOFF REQUIRED` to the least-used |
| 7d used ≥ 98% | none | `QUOTA STOP` |
| 5h used ≥ 99% | one with ≥ 20% routing left | `QUOTA HANDOFF REQUIRED` |
| 5h used ≥ 99%, week ≥ 10% left | none with ≥ 20% | `QUOTA WAIT` for the 5h reset |
| 5h used ≥ 99%, week < 10% left | only accounts with 5–20% left / none | `QUOTA HANDOFF REQUIRED` to the least-used / `QUOTA STOP` |

Routing left uses both windows, so an account with a fresh 5-hour window but 90%
of its week spent (10% left) is not a handoff target: the session waits for its
own reset instead. Handoffs need at least two accounts; with one, the session
waits or stops.

| Directive | What the agent does | Enforcement |
|---|---|---|
| `QUOTA HANDOFF REQUIRED` | Writes a handoff document, runs `agentihooks claude-terminal --handoff`, reports where the work moved, stops | Injected on every tool call until done |
| `QUOTA WAIT` | Creates a one-shot `CronCreate` job for the 5h reset, tells the operator, stops | Every tool except `CronCreate` / `CronList` / `CronDelete` is blocked |
| `QUOTA STOP` | Tells the operator to add another account or say "keep pushing" | Every tool is blocked |
| `QUOTA PUSH` | Continues on this account until 100% | The operator typed "keep pushing" (or "push to 100") this session |

A window whose reset time has passed counts as empty, so a waiting session is
released by the reset itself.

## Handoff

```bash
agentihooks claude-terminal --handoff \
  --dir "$PWD" --name repo-handoff --prompt-file ~/scratchpad/repo/handoff/<session>.md
```

`--handoff`:

- routes the new terminal to any account except this session's
  (`--agentihooks-exclude <slug>` on the inner `agenti`);
- never falls back to bare Claude;
- waits for the new session to report its route (`route_status=routed`,
  `account=<slug>`), then marks this session `handed_off` in the registry;
- exits 3 with `handoff=failed` when the new session could not be routed.

After `handoff=done` every tool call in the old session is blocked with a message
naming the account that took over.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AGENTIHOOKS_MAX_SESSIONS_PER_ACCOUNT` | `2` | Live sessions per account before launches move to the next account |
| `AGENTIHOOKS_HANDOFF_WEEK_PCT` | `98` | 7-day used % that triggers the policy |
| `AGENTIHOOKS_HANDOFF_5H_PCT` | `99` | 5-hour used % that triggers the policy |
| `AGENTIHOOKS_HANDOFF_MIN_LEFT` | `20` | Routing left % a handoff target needs |
| `AGENTIHOOKS_WAIT_MIN_WEEK_LEFT` | `10` | 7-day left % that makes a spent 5h window wait instead of stop |
| `QUOTA_POLICY_ENABLED` | `true` | Turn the quota policy off |

Set them in the shell or in `~/.agentihooks/.env`.
