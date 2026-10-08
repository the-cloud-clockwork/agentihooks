---
title: "Claude Account Load Balancing"
parent: The Four Pillars
nav_order: 7
permalink: /docs/pillars/load-balancing/
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

`agenti` (alias of `agentihooks claude`) and `agentihooks init-agent` launch
Claude on one of them. The launched session keeps only its own
`AH_CC_TOKEN_<slug>`; that variable name is how every tool below tells which
account a session runs on. Token values are never printed or logged.

## Launch routing

`agenti` probes every account (results cached 60 s in
`~/.agentihooks/claude-router-cache.json`) and places the session by one rule,
the same one the swarm uses for its seats:

1. Each account's cap of live sessions comes from its five hour window alone:
   6 at 60% or more left, 4 at 40 to 60%, 3 at 10 to 40%, 2 at 5 to 10%, none
   below 5% until that window resets.
2. An account with under 5% of its week left takes no new session until the week
   resets.
3. A reading that is missing or older than fifteen minutes is refreshed first; an
   account still without a fresh reading takes no new session.
4. The next session goes to the eligible account with the fewest live sessions
   (ties in account order), counted from every live process on the machine.

When no account has a free place the launch fails. `agentihooks balance` and the
Quota panel on the ledger page show each account's live sessions against its
computed cap. A lock held until Claude starts keeps two simultaneous launches
from both taking the last free place.

`agenti --route <slug>` skips all of this and uses `AH_CC_TOKEN_<slug>`.

```text
[agenti] account=work routing_left=62% 5h_left=80% 7d_left=62% sessions=1/6 source=cached
```

## Skill evaluations

Run Claude skill evaluations through `agentihooks skill eval` from the evaluation
runner's directory. It loads the full account set the way launches do: the launch
credentials, then every `AH_CC_TOKEN_<slug>` variable the operator's interactive
login shell (`bash -lic`) exports, so a Codex engineer with no account variables and
a Claude session holding only its own token route across every account. Values pass
by variable and are never printed. It then selects an account using the same quota and session cap rules as
launches, and exports its OAuth token through the child environment. It exits with
status 3 when no account can route. `agentihooks-skill-eval` remains as an alias
console script with the same arguments.

For the installed skill creator's trigger runner:

```bash
agentihooks skill eval \
  --agent claude \
  -- python3 -m scripts.run_eval \
  --eval-set "$EVAL_SET" \
  --skill-path "$SKILL_PATH" \
  --model haiku \
  --num-workers 1
```

`agentihooks skill eval` writes `[skill-eval] account=<slug>` to stderr as the account proof;
the runner's stdout and arguments stay intact. Every Claude subprocess started
by that runner inherits the selected account. The default Claude login is unused.
Use `--agent codex` to execute a Codex evaluation command with its arguments and
environment unchanged.

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

Handoff targets use their quota band caps alone; the policy has no default
sessions per account setting. A stale reading carries an unknown cap until the
launcher refreshes it and applies the placement rule.

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
| `QUOTA HANDOFF REQUIRED` | Writes a handoff document, runs `agentihooks init-agent --handoff`, reports where the work moved, stops | Injected on every tool call until done |
| `QUOTA WAIT` | Creates a one-shot `CronCreate` job for the 5h reset, tells the operator, stops | Every tool except `CronCreate` / `CronList` / `CronDelete` is blocked |
| `QUOTA STOP` | Tells the operator to add another account or say "keep pushing" | Every tool is blocked |
| `QUOTA PUSH` | Continues on this account until 100% | The operator typed "keep pushing" (or "push to 100") this session |

A window whose reset time has passed counts as empty, so a waiting session is
released by the reset itself.

## Early swarm quota warnings

The swarm tick checks its account quota readings and sends each unfinished agent
one `QUOTA HANDOFF WARNING` through its inbox when either window reaches the
warning threshold. Defaults are **90% of the week used** or **95% of the five hour
window used**, for Claude and Codex accounts. Missing or unknown readings do not
trigger a warning. If both thresholds are reached, the warning names the week.

These warnings give the agent time to finish its current step and write its
Handoff v2 with the handoff skill. The agent submits the document with the quota
reason, then stops:

```bash
agentihooks swarm "$SWARM" handoff "$HANDOFF_DOC" \
  --reason quota
```

The warning itself neither blocks tools nor terminates the running agent. The
hard quota policy above still applies at its separate thresholds. A quota handoff
keeps the seat and task for the successor.

### Successor account selection

For a quota handoff, the tick excludes the predecessor's account and considers
accounts allowed by the successor's lane, task reservation and required harness. A candidate
must have a free session slot, known quota for both windows, and usage below both
warning thresholds. Accounts already at a warning threshold are excluded even
when their normal launch cap still has room.

Among eligible Claude accounts, the tick chooses the one with the most routing
left: the smaller of its five hour and weekly percentages left. Equal readings
are resolved by account name. If no Claude account qualifies, it considers Codex
accounts by the same ranking, provided the lane and required harness permit
Codex and the profile has no required Claude only plugin. With no eligible
account, successor placement fails; it does not launch on a draining account.

### Worked example

With the default settings, a Claude engineer on `work` has 10% of its week left
and 60% of its five hour window left. Its weekly usage is exactly 90%, so it gets
an early warning while still below the hard weekly threshold of 98% used. It
finishes the current step, writes its Handoff v2 and submits it with `--reason
quota`.

Assume the lane permits both harnesses, no task reservation fixes the harness,
the profile has no required Claude only plugin, and every candidate below has a
free session slot and fresh readings:

| Account | Harness | Five hour left | Weekly left | Selection |
|---|---|---|---|---|
| `work` | Claude | 60% | 10% | Excluded as the predecessor and already warned |
| `personal` | Claude | 80% | 40% | Eligible with 40% routing left |
| `spare` | Claude | 70% | 60% | Selected with 60% routing left |
| `default` | Codex | 90% | 85% | Fallback if no Claude account qualifies |

The successor takes the same seat and task on `spare`. If `personal` and `spare`
also reach a warning threshold before placement, `default` is the eligible Codex
fallback despite having more routing left than either Claude account initially.

## Handoff

```bash
agentihooks init-agent --handoff \
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

## Codex accounts

Codex routes the same way. Its accounts are the default `codex login` on this
machine (named `default`, detected with `codex login status`) and one ChatGPT
workspace access token per `AH_CX_TOKEN_<slug>`.

- `agentihooks codex [--route <slug>] [codex args]` picks by the same rule, with
  each Codex account judged on its week alone with the top band (6 sessions, none
  under 5% of the week). A reading older than fifteen minutes is refreshed by one
  tiny `codex exec` on that account before placing; `--route` forces one. `init-agent --agent
  codex` and the swarm tick launch through it, and the route report names the
  account for the swarm panel.
- A token account's child keeps only its `AH_CX_TOKEN_<slug>`, loses every
  `AH_CC_TOKEN_*`, and runs `codex --no-daemon` with the token in
  `CODEX_ACCESS_TOKEN`. Codex keeps one app-server daemon per Codex home and that
  daemon answers with its own login, so a token session must not attach to it.
  The Codex home, config and hooks stay the shared ones.
- The default account runs plain `codex` with no token variables. With no
  `AH_CX_TOKEN_*` set, every launch takes this path without checking the login.
- Live sessions are counted per account from `/proc`: a Codex process with
  exactly one `AH_CX_TOKEN_<slug>` is on that account, any other on `default`.
  Quota per account is the newest rate-limit event in the session logs the
  session registry attributes to it; a token with no session yet shows no quota
  and still routes.
- `agentihooks balance` and `agentihooks quota` list every Codex account with its
  sessions and quota; a signed-out default login shows `SIGNED_OUT`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AGENTIHOOKS_HANDOFF_WARN_WEEK_PCT` | `90` | Weekly used % that triggers an early swarm handoff warning |
| `AGENTIHOOKS_HANDOFF_WARN_5H_PCT` | `95` | Five hour used % that triggers an early swarm handoff warning |
| `AGENTIHOOKS_HANDOFF_WEEK_PCT` | `98` | 7-day used % that triggers the policy |
| `AGENTIHOOKS_HANDOFF_5H_PCT` | `99` | 5-hour used % that triggers the policy |
| `AGENTIHOOKS_HANDOFF_MIN_LEFT` | `20` | Routing left % a handoff target needs |
| `AGENTIHOOKS_WAIT_MIN_WEEK_LEFT` | `10` | 7-day left % that makes a spent 5h window wait instead of stop |
| `QUOTA_POLICY_ENABLED` | `true` | Turn the quota policy off |

Set them in the shell or in `~/.agentihooks/.env`.

Warning thresholds must be positive and strictly below their corresponding hard
handoff thresholds. For example, setting the weekly hard threshold to `92`
requires a weekly warning threshold below `92`. The swarm rejects invalid
threshold pairs. `QUOTA_POLICY_ENABLED` controls the hook's hard quota policy;
it does not disable the swarm tick's early warnings.
