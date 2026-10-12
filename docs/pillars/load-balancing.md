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

### Loading accounts from OpenBao

`agentihooks init` adds one line to its managed `~/.bashrc` block that runs
`eval "$(agentihooks accounts env)"` in every new shell. The command reads the
KV v2 secret `agentihooks/claude-creds` on the `secret` mount with
`OPENBAO_URL` and `OPENBAO_TOKEN`. Each key is an account's variable name,
`account_names.token_env` of its email: `AH_CC_TOKEN_<slug>`, the slug being the
lower cased email with `@` as `at` and every other symbol as `_`
(`Ops.Two@Example.com` becomes `AH_CC_TOKEN_ops_twoatexample_com`). It prints one
`export <key>=...` per key under that same name; keys that are not lower
case `AH_CC_TOKEN_` names are skipped. Nothing is written to disk.

When OpenBao is unset or unreachable it falls back to `~/.agentihooks/accounts/claude.env` (under `AGENTIHOOKS_HOME` when
set): an operator written file of `AH_CC_TOKEN_*=` lines, used only when no one
but its owner can read it (`chmod 600`). It sits in a subfolder so the
`agentienv` and hook env loaders, which read `*.env` at the top level, never load
it. One short stderr line names the outcome whenever `OPENBAO_URL` is set or the
fallback file exists; a machine with neither starts its shells silently and
without accounts.

Codex accounts load the same way from the KV v2 secret
`agentihooks/codex-creds`, the one `codex login` and the refresher write
(`AGENTIHOOKS_CODEX_CREDS_PATH` in `~/.agentihooks/.env` names another path for
all three). A key holding an account email is exported
as `account_names.codex_token_env` of it: `AH_CX_TOKEN_` plus the same slug rule
(`Ops.Two@Example.com` becomes `AH_CX_TOKEN_ops_twoatexample_com`). A lower case
`AH_CX_TOKEN_<slug>` key, the form `codex login` and the refresher write, is
exported under its own name and wins when both name the same account. The
Codex router routes among them. `AH_CX_REFRESH_<slug>` keys and every other key
are never exported, so a shell holds access tokens only: the one machine with
`AGENTIHOOKS_AICODE_TOKEN_REFRESH=true` rotates refresh tokens and writes them
back to that secret (see Refreshing tokens). The fallback is
`~/.agentihooks/accounts/codex.env`, under the same rules as `claude.env`; its
stderr line shows under the same rule.

## Launch routing

`agenti` probes every account (results cached 60 s in
`~/.agentihooks/claude-router-cache.json`) and places the session by one rule,
the same one the swarm uses for its seats. Each probe is one 1-token Messages
request with the account's token (`POST /v1/messages`, 20 s timeout, accounts
in parallel) whose `anthropic-ratelimit-unified-5h-*` / `-7d-*` / `-status`
headers give the windows (a window whose own `-status` is `rejected` blocks the
account); a 429 carrying those headers reads like a 200. Measured live on five
accounts: 0.6 to 0.9 s per request, 2 to 3 s for all five in parallel, against
about 4 s per account for the old `claude -p` probe. A timeout (the caller's,
capped at 20 s), a broken response or a token that is not a valid header value
becomes an error for that account only; a network failure falls back to a
`claude -p` run, and `--include-fable` keeps the `claude -p` probe, since no
response header carries the fable window.

1. Each account's cap of live sessions comes from its five hour window alone:
   6 at 60% or more left, 4 at 40 to 60%, 3 at 10 to 40%, 2 at 5 to 10%, none
   below 5% until that window resets.
2. An account with under 5% of its week left takes no new session until the week
   resets.
3. A reading that is missing or older than fifteen minutes is refreshed first; an
   account still without a fresh reading takes no new session.
4. The next session goes to the eligible account with the fewest live sessions,
   counted from every live process on the machine.
5. Among accounts with equally few sessions, the one whose week resets soonest
   goes first, so quota that would expire unused is spent first. An account with
   5% or less of its five hour window left, or with no upcoming week reset, sorts
   after every account that has one. Remaining ties go by harness, then account
   name.

When no account has a free place the launch fails. `agentihooks balance` and the
Quota panel on the ledger page show each account's live sessions against its
computed cap. A lock held until Claude starts keeps two simultaneous launches
from both taking the last free place.

`agenti --route <slug>` skips all of this and uses `AH_CC_TOKEN_<slug>`.

```text
[agenti] account=work routing_left=62% 5h_left=80% 7d_left=62% sessions=1/6 source=cached
```

## Slots

The router places a launch on a slot: one harness, one account, a session cap
and its live sessions. Every source of credentials offers slots of one kind.

| Kind | Harness | Account | Cap |
|---|---|---|---|
| `subscription` | Claude | each `AH_CC_TOKEN_<slug>` | its quota band (above) |
| `subscription` | Codex | each `AH_CX_TOKEN_<slug>` | 6 with 5% or more of the week left, else 0; needs a reading under fifteen minutes old |
| `interactive` | Codex | the default `codex login`, named `default` | the same as a Codex token |
| `api` | Claude or Codex | `api`, one per harness when an endpoint is configured | `<harness>-api-max-sessions`, unbounded when unset |

Subscription and interactive slots make up the pool. An api slot has no quota
windows; only its session cap limits it. The slug `api` is reserved: routing
ignores `AH_CC_TOKEN_api` and `AH_CX_TOKEN_api`. `agenti --route api` and
`agentihooks codex --route api` force the api slot, bypass its cap and weight,
and fail when no endpoint is configured.

### Api detection per harness

Claude Code picks its credential by a fixed order: cloud provider variables,
then `ANTHROPIC_AUTH_TOKEN`, then `ANTHROPIC_API_KEY`, then `apiKeyHelper`, then
`CLAUDE_CODE_OAUTH_TOKEN`, then the `/login` subscription
([Claude Code authentication precedence](https://code.claude.com/docs/en/authentication#authentication-precedence)).
The router offers a Claude api slot only for these environments, each of which
outranks the OAuth token:

| Environment | Provider label |
|---|---|
| `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX` or `CLAUDE_CODE_USE_FOUNDRY` set to `1`, `true`, `yes` or `on` | `bedrock`, `vertex`, `foundry` |
| `ANTHROPIC_BASE_URL` on a host other than `api.anthropic.com`, with `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY` | `gateway` |
| `ANTHROPIC_API_KEY` alone | `anthropic-key` |

`ANTHROPIC_AUTH_TOKEN` without a foreign `ANTHROPIC_BASE_URL` offers no slot, and
an `apiKeyHelper` setting is not detected.

Codex signs in with a ChatGPT login, an access token or an API key
([Codex authentication](https://developers.openai.com/codex/auth)). The router
offers a Codex api slot when `CODEX_API_KEY` or `OPENAI_API_KEY` is set, in that
order. The label is `gateway` when `AH_CX_API_BASE_URL` or `OPENAI_BASE_URL` is
also set, else `openai-key`. An api launch runs `codex --no-daemon` with
per-launch `-c` overrides that select a provider named `agentihooks-api`, whose
`base_url` is that variable or `https://api.openai.com/v1`, whose `env_key` names
the key variable and whose `wire_api` is `responses`
([Codex configuration reference](https://developers.openai.com/codex/config-reference)).
The Codex config file is never written. A base URL that carries a user, a
password, a query or a fragment is refused.

### Live share weight

The routing setting `<harness>-api-weight` (0 to 100, default 0) is the share of
live sessions the api side should hold. Each launch is decided from the sessions
live at that moment:

1. With no api slot, the launch goes to the pool.
2. With no pool slot that has a free place, the launch goes to the api while the
   api is below its cap, whatever the weight, and fails when it is not.
3. Otherwise the launch goes to the api when
   `api_live / (api_live + pool_live + 1)` is below `weight / 100` and the api is
   below its cap; else to the pool.

`api_live` is the live sessions on the api account; `pool_live` is the live
sessions on every token account of that harness (the Codex default login
included), whether or not the account currently offers a slot.

Weight 0 keeps the api as an overflow for a full pool. Weight 100 sends every
launch to the api while it has room. The share is recomputed from live sessions,
so when sessions end the next launches restore it.

Worked example: five Claude tokens, all in the top band (6 places each, 30 in
the pool), weight 25, no api cap, nothing running. Twelve launches in a row
place as follows:

| Launch | api live | pool live | Share before | Side |
|---|---|---|---|---|
| 1 | 0 | 0 | 0 / 1 = 0.00 | api |
| 2 | 1 | 0 | 1 / 2 = 0.50 | pool |
| 3 | 1 | 1 | 1 / 3 = 0.33 | pool |
| 4 | 1 | 2 | 1 / 4 = 0.25 | pool |
| 5 | 1 | 3 | 1 / 5 = 0.20 | api |
| 6 to 8 | 2 | 3 to 5 | 2 / 6 = 0.33 to 2 / 8 = 0.25 | pool |
| 9 | 2 | 6 | 2 / 9 = 0.22 | api |
| 10 to 12 | 3 | 6 to 8 | 3 / 10 = 0.30 to 3 / 12 = 0.25 | pool |

The api holds 3 of the 12 sessions, one in every four launches. The 9 pool
sessions spread over the five tokens by fewest live sessions and the soonest
resetting week. Once all 30 pool places are taken, every further launch goes to
the api (rule 2).

A routing settings store that cannot be read, or a stored value that fails
validation, closes the api side for that launch with a line on stderr
(`[claude] api side closed: ...`); the pool still routes.

### Api in the quota policy

A session on the api is never subject to the quota policy below: it is never
handed off, made to wait or stopped. For a spent subscription session, an api
slot below its cap is the successor of last resort. Pool targets come first,
including the least-used account with 5% or more routing left wherever the
table below hands off to it; only where the policy would otherwise answer
`QUOTA WAIT` or `QUOTA STOP` does it hand off to the api instead, unless the
operator said "keep pushing".

## Environment isolation

Each launch gets a child environment holding exactly one credential, so the
credential precedence above cannot pick a different one than the router chose.
Values are copied by variable name and never printed.

| Child | Removed | Set |
|---|---|---|
| Claude token | `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `ANTHROPIC_BEDROCK_BASE_URL`, `AWS_BEARER_TOKEN_BEDROCK`, `ANTHROPIC_CUSTOM_HEADERS`, every `CLAUDE_CODE_USE_*`, `ANTHROPIC_VERTEX_*` and `ANTHROPIC_FOUNDRY_*`, `ANTHROPIC_BASE_URL` unless its host is `api.anthropic.com`, `AH_ROUTE_API`, every other `AH_CC_TOKEN_*` | `CLAUDE_CODE_OAUTH_TOKEN` and its own `AH_CC_TOKEN_<slug>` |
| Claude api | `CLAUDE_CODE_OAUTH_TOKEN`, every `AH_CC_TOKEN_*` | `AH_ROUTE_API=1` |
| Codex token | `CODEX_API_KEY`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `AH_ROUTE_API`, `CODEX_ACCESS_TOKEN`, every `AH_CX_TOKEN_*` and `AH_CC_TOKEN_*` | `CODEX_ACCESS_TOKEN` and its own `AH_CX_TOKEN_<slug>` |
| Codex default login | the same as a Codex token child | nothing |
| Codex api | `CODEX_ACCESS_TOKEN`, every `AH_CX_TOKEN_*` and `AH_CC_TOKEN_*` | `AH_ROUTE_API=1` |

A Claude launch also sets `AGENTIHOOKS_ROUTE_ACCOUNT` to the account it chose
(`api` for the api side).

## Route markers

Attribution reads variable names only, never values. A process whose
environment holds `AH_ROUTE_API` counts on the account `api`; otherwise it counts
on the one `AH_CC_TOKEN_<slug>` (Claude) or `AH_CX_TOKEN_<slug>` (Codex) it holds,
and on `unrouted` (Claude) or `default` (Codex) with none or several. The launch
line names the side:

```text
[agenti] account=api kind=api sessions=1/1000000 source=cached
[agentihooks codex] account=api sessions=0/1000000 placement=open
```

An unbounded api cap prints as `1000000` on the launch line and as `none` in the
`CAP` column. A forced launch prints `[agenti] account=api route=forced` for
Claude and `sessions=<n>/?` for Codex.

## Routing settings

Weights and api caps live in the routing settings store: a Redis hash when the
swarm Redis answers, else `routing-settings.json` under `$AGENTIHOOKS_HOME`
(default `~/.agentihooks`). Every write
records its actor and time in the store's history.

| Key | Values | Default | Meaning |
|---|---|---|---|
| `claude-api-weight`, `codex-api-weight` | integer 0 to 100 | `0` | Live share of the api side |
| `claude-api-max-sessions`, `codex-api-max-sessions` | integer 0 or more | unset (unbounded) | Session cap of the api slot; `0` closes the api side to placement (a forced `--route api` still launches) |
| `master-account-claude`, `master-account-codex` | account name | unset | Account declared for swarm masters; stored and validated, read by no launch path yet |
| `master-tier-claude`, `master-tier-codex` | tier name | unset | Tier declared for swarm masters; stored and validated, read by no launch path yet |

```bash
agentihooks balance settings                     # every key with its value and the store in use
agentihooks balance set claude-api-weight=25
agentihooks balance set claude-api-max-sessions=4 codex-api-weight=10
agentihooks balance set claude-api-max-sessions=none   # clear: back to the default
```

`balance set` validates every pair before it writes any, prints the store and
one `key: before -> after` line per key, and exits 2 on an unknown key or an
invalid value. The actor is `AGENTIHOOKS_AGENT_NAME`, else `operator`.
`agentihooks balance` and `agentihooks quota` show each row's `KIND`, `WEIGHT`
and `CAP`; an api row shows `n/a` for its quota windows.

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
`claude -p`) is attributed to `api` when it holds `AH_ROUTE_API`, else to the one
`AH_CC_TOKEN_<slug>` name in its environment, or to `unrouted` when it has none.
A session that handed its work off (below) no longer holds a slot.

```text
$ agentihooks balance
#  ACCOUNT        KIND          STATE   SESSIONS  WEIGHT  CAP  ROUTING LEFT  5H LEFT  5H RESET  7D LEFT  7D RESET
-  -------------  ------------  ------  --------  ------  ---  ------------  -------  --------  -------  --------
1  personal       subscription  NORMAL  1/6       -       6    99%           99%      1h20m     99%      6d00h
2  work           subscription  NORMAL  2/6       -       6    54%           81%      30m       54%      5d10h
-  api (gateway)  api           OPEN    1/4       25%     4    n/a           n/a      n/a       n/a      n/a
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
window used**, for Claude and Codex accounts. Each known window is checked
independently; an unknown window does not suppress a warning from the other one.
Accounts with an unknown quota state do not trigger a warning. If both thresholds
are reached, the warning names the week.

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
must have a free session slot, a known quota state and at least one known window,
with usage below the warning threshold in every known window. Accounts already
at a warning threshold are excluded even
when their normal launch cap still has room.

Among eligible Claude accounts, the tick chooses the one with the most routing
left: the smaller of its known five hour and weekly percentages left, or the
weekly percentage alone when Codex reports only that window. Equal readings
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
| `default` | Codex | Unknown | 85% | Eligible with 85% routing left; fallback if no Claude account qualifies |

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

Codex routes the same way. Its accounts are one ChatGPT workspace access token
per `AH_CX_TOKEN_<slug>`. Only when no `AH_CX_TOKEN_*` is set is the account the
default `codex login` on this machine (named `default`, detected with
`codex login status`); once any token account exists, `default` leaves the
routing pool and the `agentihooks quota` rows. A signed out `default` gets no
seat from automatic placement or the swarm tick, and `--route default` refuses
it by name, so every Codex seat the tick offers is one `agentihooks codex
--route` accepts.

- `agentihooks codex [--route <slug>] [codex args]` picks by the same rule, with
  each Codex account judged on its week alone with the top band (6 sessions, none
  under 5% of the week). A reading older than fifteen minutes is refreshed
  before placing, at most once per fifteen minutes per account: a ChatGPT token
  account, or a default login whose `auth.json` holds its token, gets a free
  read of the ChatGPT usage endpoint (`/backend-api/wham/usage`, no inference
  spent), cached with mode 0600 in `codex-usage.json` under `AGENTIHOOKS_HOME`
  with the windows, plan and limit only, keyed by the account slug. Any other
  account, and a read that fails for any reason but an expired token, gets one
  tiny `codex exec`. A read answered `401 token_expired` marks the account
  `EXPIRED`: no seats, and `agentihooks quota` names the login that renews it.
  A limit reached before the 7-day reset stops counting once that reset passes. Until then the cap keeps reading an older one while it
  is younger than `AGENTIHOOKS_CODEX_READING_MAX_AGE_H` hours (default 6), so an
  idle account keeps its seats; a 7-day window that has reset in the meantime
  reads 100% left. `agentihooks quota` shows `STALE` only past that age, and
  judges a reading older than fifteen minutes on its week alone, without its old
  five-hour window.
  `--route` forces one. `init-agent --agent
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
  An interactive session and a `codex exec` run each hold one slot until the
  process ends. The router's quota probes, login checks and classifier calls
  carry `AGENTIHOOKS_INTERNAL_RUN` and hold none, nor does a bare `codex` they
  start, which inherits it, nor do `app-server` and
  `mcp-server` processes; a routed launch drops that marker from its child, so
  it counts even when started inside one of them. A Codex process whose parent
  is a Codex process belongs to that session. A run started through a shell,
  even inside a Codex session, holds its own slot.
  Quota per account is the newest of the usage read and the rate-limit event in
  the session logs the session registry attributes to it; a token with neither
  shows no quota and still routes.
- `agentihooks balance` and `agentihooks quota` print one table for every Claude
  and Codex account: `# HARNESS ACCOUNT KIND STATE SESSIONS WEIGHT CAP ROUTING
  LEFT 5H LEFT 5H RESET 7D LEFT 7D RESET`, Claude rows first, then Codex, each
  harness sorted by routing left, subscription rows numbered 1..N across the
  whole table (api and interactive rows unnumbered), `-` for any missing value.
  Codex routing left is its week left whenever the week is known. A signed-out
  default login shows `SIGNED_OUT` and a cancelled token `EXPIRED` with `-`
  values. `balance --current` marks this session's account `(current)`. Sessions on
  names that are not listed accounts are counted in one last line,
  `+N sessions on unlisted account names`, only when there are any. `quota
  --json` gives the same fields per row with `harness`. `balance --fable` prints
  the Claude rows (api slot included) with `FABLE LEFT` and `FABLE RESET`
  columns.
- The swarm tick, the controller and `init-agent` never source the shell rc, so
  they read the `AH_CC_TOKEN_*` and `AH_CX_TOKEN_*` export lines of the managed
  token block that `codex login` and `claude login` write, and nothing else in
  that file. The block value wins for a name the environment already holds (it
  is the refreshed one); a name only in the environment stays. Every account
  list (`balance`, `quota`, the tick, routing) counts a token value once: when
  several names hold the same token, the block's name is kept, else the first
  name in sort order.

### Logging in an account

`agentihooks codex login <slug> [--device-auth] [--openbao | --no-openbao]` logs
one ChatGPT workspace in and exports it. A ChatGPT Codex access token lives ten
days, so the refresher below keeps it alive.

- The slug follows the `accounts env` rule: lower cased, `@` becomes `at` and
  every other character outside `a-z` and `0-9` becomes `_`, one for one, so an
  email works (`nestor.colt@gmail.com` becomes `nestor_coltatgmail_com`, `team-a`
  becomes `team_a`) and the login prints a note recommending the email of the
  target account. A result that is empty, starts with `_`, or is `status`,
  `help`, `default` or `api` is refused. Both logins use this rule. An account
  stored under an older mixed-case name (`ncgmaTCC`) keeps routing on its
  current access token, but `codex refresh` no longer renews it, so it stops
  working when that token expires; the refresher names it on every run.
  `codex refresh --force <name>` still renews it once under the old name. The
  fix is a fresh browser sign-in with `agentihooks codex login <old name>`,
  which stores it under the new slug and removes the old keys. A name that
  cannot become a slug at all is named the same way and needs a login under a
  new slug.
  `agentihooks codex login` with no slug, `login status`, `login help` and
  `login --<flag>` still go straight to `codex login`.
- The login runs `codex login -c cli_auth_credentials_store="file"` (browser
  flow, or the device flow with `--device-auth`) with `CODEX_HOME` set to
  `~/.agentihooks/codex/<slug>` (mode 0700), `AGENTIHOOKS_INTERNAL_RUN=1`, and
  no other Codex token, account id or api key in its environment. A failed login
  exits with Codex's code and writes nothing.
- On success it writes `export AH_CX_TOKEN_<slug>=<access token>` and
  `export AGENTIHOOKS_AICODE_TOKEN_REFRESH=true` into the managed block of the
  shell rc file (`~/.zshrc` on macOS, `~/.bashrc` elsewhere), between
  `# ===AGENTIHOOKS-AICODING-TOKENS===` and
  `# ===AGENTIHOOKS-AICODING-TOKENS-END===`. A line is replaced in place, a
  missing one is added, every other line is kept, and the block is appended
  when absent; the write is atomic and keeps the file's mode. It then prints the
  workspace, plan, expiry and the file to `source`, never a token value.
- OpenBao: `--openbao` stores without asking, `--no-openbao` skips. Otherwise a
  terminal is asked whether to store them, then whether to use the detected
  `OPENBAO_URL` and `OPENBAO_TOKEN`, or for a URL and a token typed with no
  echo; typed credentials serve that one write and are saved nowhere. Off a
  terminal with neither flag the step is skipped. When the OpenBao write fails
  or is refused (`--openbao` off a terminal without both variables), the login
  exits 1 after the block was already written.
- Every account lives in one KV v2 secret, `secret/agentihooks/codex-creds`
  unless `AGENTIHOOKS_CODEX_CREDS_PATH` names another path, under the keys
  `AH_CX_TOKEN_<slug>` and `AH_CX_REFRESH_<slug>`. A write merges
  its keys over the current ones with check-and-set, retried up to three times
  on a conflict, and reports key names and the key count only.

### Logging in a Claude account

`agentihooks claude login [slug] [--openbao | --no-openbao]` captures the
one-year token `claude setup-token` prints and stores it the same way; every
other `agentihooks claude ...` still routes and launches Claude.

- With no slug, a terminal is asked for one (the email of the target account);
  off a terminal the command prints its usage and exits 2.
- `claude setup-token` runs in a pseudo-terminal 1000 columns wide, so the token
  prints on one line instead of being wrapped, with keystrokes and output passed
  straight through so the browser sign-in works as usual. The terminal mode is
  restored however the run ends, including on `SIGTERM` or `SIGHUP`, which also
  kill `setup-token`; a `setup-token` still running ten seconds after its output
  closes is killed. `setup-token` signs in whichever account the browser holds,
  so use a private window for the account you want.
- The `setup-token` screen itself shows the token: that is the child's output,
  passed through, never agentihooks' own. Run the login in your own terminal,
  never from an agent tool.
- The token is the longest `sk-ant-oat01-…` match once escape sequences are
  stripped. No match, a non-zero exit or a `claude` that cannot start (named in
  the error) writes nothing and exits non zero.
- On success it writes `export AH_CC_TOKEN_<slug>=<token>` into the managed block
  (no refresh flag: the token lasts a year) and prints the variable, its length
  and the file to `source`, never the token.
- The OpenBao step is the one `codex login` uses, writing the key
  `AH_CC_TOKEN_<slug>` into the KV v2 secret `secret/agentihooks/claude-creds`.
  `agentihooks accounts env` reads this secret.

Both logins print their steps and notes in blue, questions and warnings in
yellow and errors in red, but only when standard output is a terminal and
`NO_COLOR` is unset (set to any value, even empty, it turns colour off);
otherwise the text is plain. Errors go to standard error, coloured by the same
rule. The slug note names the variable of its own command (`AH_CC_TOKEN_` for
Claude, `AH_CX_TOKEN_` for Codex). `codex refresh` colours its warnings and
failures the same way.

### Refreshing tokens

`agentihooks codex refresh [--force <slug>] [--days N]` renews each account whose
access token has fewer than `--days` days left (default
`AGENTIHOOKS_AICODE_TOKEN_REFRESH_DAYS`, else 3); `--force` renews one account
now. Each grant returns a new refresh token, so exactly one machine may refresh
an account: the refresher runs only where `AGENTIHOOKS_AICODE_TOKEN_REFRESH=true`
and otherwise exits 0. A login sets that flag, so the machine a person logged in
on becomes the refresh authority.

- `AGENTIHOOKS_AICODE_TOKEN_STORE=local` (default): the accounts are the
  `~/.agentihooks/codex/<slug>/auth.json` files. A refresh writes that file
  first, then the rc block, then OpenBao when `OPENBAO_URL` and `OPENBAO_TOKEN`
  are set; when it saved a new pair locally without them, the run says OpenBao
  was not updated. A write error on one account is reported and the others
  still refresh; a pair that could not be saved locally still goes to OpenBao.
- `AGENTIHOOKS_AICODE_TOKEN_STORE=openbao`: the accounts are the
  `AH_CX_REFRESH_<slug>` keys of the OpenBao secret, written back there; the rc
  block is updated only where it already exists. It refuses to run without
  `OPENBAO_URL` and `OPENBAO_TOKEN`. When the OpenBao write fails after a
  refresh, the new pair is kept in `~/.agentihooks/codex/<slug>/recovery.json`
  (0600) and the error names that file. The next run uses that pair when it is
  newer than the one in OpenBao, stores it and removes the file; an older one
  holds a spent refresh token and is removed. This store never reads or changes
  a login's `auth.json`.
- A new pair that nothing could keep (no local file, no OpenBao) prints
  `<slug>: new token pair not persisted; run agentihooks codex login <slug>`.
- Each account prints its days left, then `refreshed` with the new days left and
  whether the refresh token rotated. A refused grant prints the slug, the HTTP
  status and the OAuth error and the run moves on, exiting non zero.
- A second run on the same machine skips while the first holds
  `<AGENTIHOOKS_HOME>/codex-refresh.lock`.
- Cadence: every session start on the refresh authority starts
  `agentihooks codex refresh` detached when `<AGENTIHOOKS_HOME>/codex-refresh.stamp`
  is older than `AGENTIHOOKS_AICODE_TOKEN_REFRESH_INTERVAL_H` hours (default 6).
  A machine without the flag only reads it; a start that fails removes the stamp
  so the next session retries. Without `agentihooks` on `PATH` the kick logs
  `codex refresh kick skipped: agentihooks is not on PATH` and retries at the
  next session start. Cron or a Kubernetes CronJob can run the same
  command.
- Known gap: when the server rotates the refresh token but its response is lost,
  the stored refresh token is spent and every later grant is refused. Recover
  with a fresh `agentihooks codex login <slug>`.

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
