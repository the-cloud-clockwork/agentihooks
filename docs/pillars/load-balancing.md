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
`AH_CC_TOKEN_<slug>`; that variable carries the token every tool below uses to
tell which account a session runs on. Token values are never printed or logged.

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

Attribution reads variable names, plus a Claude token's SHA-256 to merge
renamed accounts ([Renamed account variables](#renamed-account-variables));
values are never printed. A process whose environment holds `AH_ROUTE_API`
counts on the account `api`; otherwise it counts on the one
`AH_CC_TOKEN_<slug>` (Claude) or `AH_CX_TOKEN_<slug>` (Codex) it holds, and on
`unrouted` (Claude) or `default` (Codex) with none or several. The launch line
names the side:

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
| `master-account-claude`, `master-account-codex` | account name | unset | Account declared for swarm masters, used for interactive routing and worker reservation |
| `master-tier-claude`, `master-tier-codex` | tier name | unset | Display label for the declared master account |

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

## Master accounts and reserved headroom

Declare the account the operator uses for masters separately from worker
placement. The declaration stores account names and optional tier labels, never
credentials:

```bash
agentihooks balance master-account claude=<slug> tier=Max
agentihooks balance master-account codex=default tier=Pro
agentihooks balance settings
agentihooks balance master-account --clear claude
```

Each `tier=` follows the account it labels. Codex accepts `default` for its
native login or a slug with a matching `AH_CX_TOKEN_<slug>` variable. A Claude
declaration may name an interactive login with no worker token. Declarations
appear as `MASTER <tier>` in account reports. Clearing one harness leaves the
other declaration in place; `--clear` without a harness clears both. These
commands declare an identity; sign in separately using the
[master login procedure](../reference/account-login.md#master-claude-ai-login).

The declared account keeps weekly headroom according to how many masters are
live on it, across swarms. The 70/60/50 rule is a ceiling on **weekly quota
used**, not the percentage left and not a session cap:

| Live masters on the declared account | Worker ceiling: week used | Headroom left at the ceiling |
|---|---|---|
| 0 | No master reservation | Normal quota rules apply |
| 1 | 70% | 30% |
| 2 | 60% | 40% |
| 3 or more | 50% | 50% |

At or above the ceiling, new workers and worker quota handoffs cannot select
that account. A running worker's quota policy uses the same ceiling. Masters
are exempt from the worker reservation; their normal account eligibility and
quota limits still apply. Other accounts keep their normal routing rules, and
workers can use their tokens or the configured API capacity.

For example, with one master and 65% of its account's week used, workers may
still use that account. A second master lowers the ceiling to 60%, so the same
65% reading now closes it to workers. With three masters, workers stop selecting
it at 50% used. When the count drops, the next reservation reading uses the
corresponding ceiling; with no live master the reservation disappears.

A master placed on a token route sees the declared account with its original
session cap and quota state. With one live master, the declared account at 72%
of the week used with five hour room left, and every other account spent, a
worker is refused, while a new or handed off master on fallback auth is placed
on the declared account.

The account report includes the reason, for example
`reserved for 2 live masters: workers stop at 60% of the week`. Use
`agentihooks balance` and `agentihooks quota` to inspect the declaration and
reservation. See [Interactive masters and Remote Control](swarm.md#interactive-masters-and-remote-control)
for launch authentication, fallback and phone access.

### Quota readings for an interactive master account

A Claude master account declared with no `AH_CC_TOKEN_<slug>` variable is an
interactive login: the balancer has no token to probe, so the router cache
holds no reading for it. Swarm capacity reads its quota from the master's own
sessions instead:

1. The status line writes each session's snapshot to
   `~/.agentihooks/quota_usage/<session id>.json` (see
   [Quota usage banner](#quota-usage-banner)). When the session's environment
   holds exactly one non empty `AH_ROUTE_INTERACTIVE_<slug>` variable, the
   snapshot also records `"account": "<slug>"`. A session holding none,
   several, an empty value or the bare prefix records no account.
2. For the declared Claude master account of kind interactive, swarm capacity
   takes the freshest snapshot naming that slug that is no older than
   `QUOTA_USAGE_STALE_SEC`. Unreadable and malformed files are skipped.
3. That snapshot becomes the account's reading: the master row carries its
   5-hour left, week left, both reset times and the time the snapshot was
   written. A snapshot missing one window reads that window as unknown.

Only swarm capacity reads these snapshots; `agentihooks balance` does not, and
lists a tokenless master account with unknown windows.

A snapshot written by an interactive master session:

```json
{
  "updated_at": 1792469429.3,
  "rate_limits": {
    "five_hour": {"used_percentage": 30.0, "resets_at": 1792476000},
    "seven_day": {"used_percentage": 60.0, "resets_at": 1792890000}
  },
  "account": "home"
}
```

With `home` declared as the Claude master account, the swarm quota view shows
`home` with 70% of its 5-hour window and 40% of its week left. The cap stays
unknown and the state `UNKNOWN`, so the swarm offers no worker seats on the
account; the row exists to carry the week left that the
[reservation](#master-accounts-and-reserved-headroom) is judged against. With
one live master and 60% of the week used the reservation does not close the
row; at 70% used it does.

The reading never applies to:

- an account that holds a token (it is probed like any other account);
- a Codex declaration, an undeclared account, or a declaration the routing
  settings store cannot load;
- a snapshot older than `QUOTA_USAGE_STALE_SEC`. The row then stays `UNKNOWN`
  with no windows, and the reservation cannot close it until a fresh
  snapshot arrives.

The row appears only while the swarm counts a live session on the account.
Interactive sessions count under their `AH_ROUTE_INTERACTIVE_<slug>` name. A
Codex master on the native login needs no snapshot: a Codex process holding
`AH_ROUTE_INTERACTIVE_default` counts under `default`, and that account's
rollout logs already supply its quota.

### Reserved rows in `agentihooks balance`

`agentihooks balance` applies the live rule the swarm quota view applies to
Claude and Codex rows, so a row with the same reading closes in both. Swarm
capacity also keeps a closed row closed until its week resets; `balance` does
not (see [Closed until the week resets](#closed-until-the-week-resets)). A row
closes when all of these hold:

- its harness and account match the declared master account of that harness;
- it is not an api row;
- its week left is known; a week whose reset time has passed counts as reset;
- the week used reaches the ceiling for the live master count.

A closed row shows state `CLOSED`, cap `0` and `(reserved)` after the account
name and its `MASTER <tier>` mark. One line per closed row under the table
names the harness, the account and the reason:

```text
$ agentihooks balance
#  HARNESS  ACCOUNT                        KIND          STATE   SESSIONS  WEIGHT  CAP  ROUTING LEFT  5H LEFT  5H RESET  7D LEFT  7D RESET  AGE
-  -------  -----------------------------  ------------  ------  --------  ------  ---  ------------  -------  --------  -------  --------  ---
1  claude   home MASTER Max (reserved)     subscription  CLOSED  2/0       -       0    -             90%      -         30%      -         -
2  codex    default MASTER Pro (reserved)  subscription  CLOSED  1/0       -       0    -             90%      -         20%      -         -
3  codex    spare                          subscription  NORMAL  0/6       -       6    -             90%      -         80%      -         -
claude home: reserved for 2 live masters: workers stop at 60% of the week
codex default: reserved for 1 live master: workers stop at 70% of the week
```

With `--fable` the table lists Claude rows only, so only the Claude line
appears. Rows from `agentihooks quota --json` carry a `reserved` key; only
`balance` applies the reservation, so `quota` leaves it empty.

If a reserved account still shows as open:

- The week left is `-`: there is no fresh reading, so no reservation can close
  the row. `balance` reads only probed and cached token readings, so a
  tokenless interactive Claude master always shows `-` there and stays open;
  its reservation is visible in the swarm quota view. For that view, check
  that a live master session holds exactly one non empty
  `AH_ROUTE_INTERACTIVE_<slug>` and that its snapshot is younger than
  `QUOTA_USAGE_STALE_SEC`.
- The account name differs from the declaration: check
  `agentihooks balance settings` for `master-account-claude` or
  `master-account-codex`.
- No master is live on the account: with zero live masters there is no
  reservation.

### Closed until the week resets

Swarm capacity, the account list the swarm tick, hive launches and agent
choice place agents from, keeps the declared master account closed for
workers from the first reading that closes it until that account's week
resets. A later reading that would reopen it under the live rule does not
reopen it. The live ceiling moves with the live master count, so without this
closure a lane agent could be placed on the account between two readings that
both close it.

On each capacity read, for each harness with a declared master account:

1. The live rule runs first, as in
   [Reserved rows](#reserved-rows-in-agentihooks-balance).
2. If the live rule closes the declared account's row and its week reset time
   is known, swarm capacity writes the reason to the Redis key
   `<prefix>:reserved-closed:<harness>:<slug>` with an expiry at that reset.
   `<prefix>` is `AGENTIHOOKS_SWARM_KEY_PREFIX`, default `agentihooks`. Each
   closing read writes the key again with the current reason.
3. If the live rule leaves the row open and the key exists, the row is shown
   `CLOSED` with cap `0` and the stored reason, the one from the last closing
   read.

Masters keep the account. The tick places a master from the same rows with the
reservation removed, so a closed row returns to its band cap and quota state
for the master lane only.

The closure ends when any of these happens:

- the week resets and the key expires;
- the master declaration for that harness is cleared with
  `agentihooks balance master-account --clear <harness>`;
- the declaration names another account; declaring the first account again
  before its reset brings the closure back, since the key is still there.

A live master count that drops, to zero included, does not end it. The closure
applies only to the declared account's non api row on its own harness. A row
with no known week reset is never stored, and a Redis read or write failure
leaves only the live rule in charge for that read, so a count drop during the
failure can still reopen the row for workers.

Example: `home` is the declared Claude master account, its week is 61% used
and two masters are live, so the live rule closes it at the 60% ceiling and the
key is written. One master ends. Under the live rule the ceiling rises to 70%
and `home` would reopen, but swarm capacity still shows it closed:

```text
$ agentihooks swarm <slug> status | grep "quota account claude home"
quota account claude home  closed  routing 39% left, reserved for 2 live masters: workers stop at 60% of the week  sessions 1
```

The line may end with the age of the reading. `agentihooks balance` reads the
live rule only, so for the same reading it can show `home` open while the tick
still refuses workers there.

Inspect or end a stored closure:

```bash
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" --scan --pattern 'agentihooks:reserved-closed:*'
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" GET agentihooks:reserved-closed:claude:home
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" TTL agentihooks:reserved-closed:claude:home
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" DEL agentihooks:reserved-closed:claude:home
```

`GET` returns the stored reason and `TTL` the seconds until the week reset.
`DEL` reopens the row to workers on the next capacity read unless the live
rule closes it again.

A lane agent's own quota handoff stays in its seat. When the quota policy
decides `QUOTA HANDOFF REQUIRED` in a session that has `AGENTIHOOKS_SWARM`
set, the directive tells it to write its Handoff v2, run
`agentihooks swarm <slug> handoff DOC --reason quota` and stop; it never names
`agentihooks init-agent --handoff`. The tick keeps the seat and the task and
routes the successor through swarm capacity, so the successor is never placed
on the closed account either. See [Quota policy](#quota-policy).

Troubleshooting:

- The tick refuses workers on the master account while `balance` shows it
  open: the stored closure holds. Read its `TTL`; it ends at the week reset.
- A lane agent landed on the master account: check that a master declaration
  exists for that harness in `agentihooks balance settings`, that the
  account's week reset is known (`7D RESET` in `agentihooks quota` is not `-`),
  and that the swarm Redis was reachable at the spawn; each missing piece
  leaves only the live rule.
- A swarm agent ran `init-agent --handoff`: check that its environment has
  `AGENTIHOOKS_SWARM`; without it the quota policy gives the plain session
  directive.

Evidence: on 19 October the Doctor tick's capacity line showed the declared
Claude master account closed at 12:07 and 12:17 UTC and open at 12:13 and
12:15, and at 12:13:52 the tick started engineer@d44a5a-0683 on it with 39% of
its week left. The agent then handed off through `init-agent --handoff` into
an unseated session. Pull request
[#5044](https://github.com/the-cloud-clockwork/agentihooks/pull/5044) added
the stored closure and the seat handoff directive; its red Mutation preflight
run 39357837089 failed the four new cases and Tests run 39363775886 passed on
the merged head. Cases live in `tests/swarm/test_reserved_latch.py` and
`tests/context/test_quota_policy.py`.

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
`claude -p`) is attributed to `api` when it holds `AH_ROUTE_API`, else to the
account its one `AH_CC_TOKEN_<slug>` value belongs to (see
[Renamed account variables](#renamed-account-variables)), or to `unrouted`
when it has none or several.
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

### Renamed account variables

Renaming a Claude account's variable, for example from `AH_CC_TOKEN_work` to
`AH_CC_TOKEN_work_example_com`, leaves sessions started before the rename
carrying the old name. The counter matches each session to an account by its
token value, so those sessions still count against the one account:

- The configured tokens are the non empty `AH_CC_TOKEN_<slug>` names in the
  environment plus the managed token block, `api` excluded. Each token counts
  once, under the name chosen by the dedupe rule shared by every account list
  (see [Codex accounts](#codex-accounts)); that name is the slug the balancer
  routes the token under.
- A session holding exactly one `AH_CC_TOKEN_<slug>` name is matched to a
  configured token by a SHA-256 of its value, computed in process. A match
  counts the session under the routed slug, whatever name it carries; no match
  counts it under its own name. A session holding none or several names stays
  `unrouted`, and one holding `AH_ROUTE_API` stays `api`.
- `agentihooks balance`, `agentihooks quota`, the quota policy and the swarm
  capacity step read this count, so on one host one token shows as one row
  with the summed sessions and one cap. Distinct tokens stay separate rows.
- The token value and its SHA-256 never leave the counter: no command, tool
  or log prints them.
- Only Claude subscription tokens are matched. Codex sessions still count by
  their `AH_CX_TOKEN_<slug>` name.

With six sessions started under `AH_CC_TOKEN_work` and both names holding the
same token, `balance` shows one row instead of `work` at six sessions beside
an empty `work_example_com` row whose cap the swarm would offer as free seats:

```text
$ agentihooks balance
#  ACCOUNT            KIND          STATE   SESSIONS  WEIGHT  CAP  ROUTING LEFT  5H LEFT  5H RESET  7D LEFT  7D RESET
-  -----------------  ------------  ------  --------  ------  ---  ------------  -------  --------  -------  --------
1  work_example_com   subscription  NORMAL  6/6       -       6    54%           81%      30m       54%      5d10h
```

If the old name still shows as its own row:

- The reading host has the old token but not the new name: neither its
  environment nor its managed token block holds `AH_CC_TOKEN_<new slug>`, so
  the old name is the only configured name for that token. Add the new name to
  that host's token block (`agentihooks claude login`, or the OpenBao load) and
  the rows merge on the next read.
- Sessions are counted across [hives](hives.md): each hive daemon matches its
  own sessions with its own configured tokens and publishes per account name.
  A hive still configured with the old name publishes it as a separate row with
  its own cap until every publishing hive holds the new name.
- The old name holds a token that was since replaced: it matches nothing and
  keeps its old name until those sessions end.

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
| `QUOTA HANDOFF REQUIRED` | Writes a handoff document, runs `agentihooks init-agent --handoff`, reports where the work moved, stops; a swarm session instead writes its Handoff v2, runs `agentihooks swarm <slug> handoff DOC --reason quota` and stops, keeping its seat | Injected on every tool call until done |
| `QUOTA WAIT` | Creates a one-shot `CronCreate` job for the 5h reset, tells the operator, stops | Injected as an alert on every tool call; no tool is blocked |
| `QUOTA STOP` | Tells the operator to add another account or say "keep pushing" | Injected as an alert on every tool call; no tool is blocked |
| `QUOTA PUSH` | Continues on this account until 100% | The operator typed "keep pushing" (or "push to 100") this session |

A window whose reset time has passed counts as empty, so a waiting session is
released by the reset itself.

## Quota usage banner

Claude Code sessions see their own remaining quota in a banner built by
`hooks/context/quota_usage.py`. Its title, `ATTENTION TO QOUTA USAGE`, is
printed with that spelling:

```text
╔══════════════════════════════════════════════════════════════════════════════╗
║  ATTENTION TO QOUTA USAGE                                                    ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  5H REMAINING: 96% | resets in 1h59m                                         ║
║  7D REMAINING: 23% | resets Sun 11:00 UTC                                    ║
║  Shown every 5 tool calls                                                    ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

| When | Injected |
|---|---|
| Every user prompt | Always, when quota numbers are available |
| PreToolUse | Every `QUOTA_USAGE_TOOL_CALLS` tool calls of the session, counted by the enforcement tool call counter; skipped on a call that already carries a quota policy directive |

Where the numbers come from, first match wins:

1. **The session's own snapshot.** The status line writes the `rate_limits`
   Claude Code reports to `~/.agentihooks/quota_usage/<session id>.json`
   (folder mode 0700, file mode 0600). SessionEnd deletes it.
2. **The router cache.** When the session carries exactly one
   `AH_CC_TOKEN_<slug>`, the banner reads that account's entry from
   `~/.agentihooks/claude-router-cache.json`.

A reading older than `QUOTA_USAGE_STALE_SEC` is ignored. With no fresh reading,
or a reading missing either window, no banner is shown; a fresh snapshot missing
a window also keeps the router cache from being read. A reset time under an
hour prints as `resets in 42m`, under a day as `resets in 1h59m`, later as the
UTC weekday and time, and as `reset unknown` when the reading has no reset
time.

Turn the banner off with `QUOTA_USAGE_INJECTION_ENABLED=false`, or change its
cadence, in the shell before the session starts or in `~/.agentihooks/.env`:

```bash
QUOTA_USAGE_INJECTION_ENABLED=true
QUOTA_USAGE_TOOL_CALLS=10
QUOTA_USAGE_STALE_SEC=300
```

`QUOTA_USAGE_STALE_SEC` also bounds the reading the [quota policy](#quota-policy)
trusts.

### Testing the banner cadence

`hooks.context.quota_usage` copies `QUOTA_USAGE_TOOL_CALLS` from `hooks.config`
when it is imported, and the PreToolUse cadence reads `hooks.config`. A test
that changes the cadence patches both with `monkeypatch`, so teardown restores
each module's own value:

```python
import hooks.config as config
import hooks.context.quota_usage as quota_usage

monkeypatch.setattr(quota_usage, "QUOTA_USAGE_TOOL_CALLS", 1)
monkeypatch.setattr(config, "QUOTA_USAGE_TOOL_CALLS", 1)
```

A test that asserts the `Shown every N tool calls` line sets the cadence it
expects instead of relying on the default:

```python
monkeypatch.setattr(quota_usage, "QUOTA_USAGE_TOOL_CALLS", 5)
```

`test_banner_renders_requested_cadence_after_injection_tests_patch_it` in
`tests/test_quota_usage.py` guards this. It runs the injection kinds test that
patches the cadence to 1, then the banner rendering test, both in one
subprocess interpreter with the xdist and cache plugins disabled, and passes
only when both exit 0. The banner test's result therefore does not depend on
which tests a CI shard ran before it.

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
  five-hour window. A `STALE` row whose 7-day window has reset since its reading
  shows `-` for `7D LEFT` and `ROUTING LEFT`: the old reading says nothing about
  the new week, so it is unknown rather than a full 100%.
  `--route` forces one. `init-agent --agent
  codex` and the swarm tick launch through it, and the route report names the
  account for the swarm panel.
- A token account's child keeps only its `AH_CX_TOKEN_<slug>`, loses every
  `AH_CC_TOKEN_*`, and runs `codex --no-daemon` with the token in
  `CODEX_ACCESS_TOKEN`. Codex keeps one app-server daemon per Codex home and that
  daemon answers with its own login, so a token session must not attach to it.
  The Codex home, config and hooks stay the shared ones; each account's quota
  readings come from its own sessions folder (see
  [Account identity and own sessions folders](#account-identity-and-own-sessions-folders)).
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
  that account's own sessions folder, or, while the folder holds no reading, in
  the session logs the session registry attributes to it; a token with neither
  shows no quota and still routes.
- `agentihooks balance` and `agentihooks quota` print one table for every Claude
  and Codex account: `# HARNESS ACCOUNT KIND STATE SESSIONS WEIGHT CAP ROUTING
  LEFT 5H LEFT 5H RESET 7D LEFT 7D RESET`, Claude rows first, then Codex, each
  harness sorted by routing left, subscription rows numbered 1..N across the
  whole table (api and interactive rows unnumbered), `-` for any missing value.
  A Codex row's `KIND` comes from the same Codex account routing offers, so the
  table, `balance`, `quota --json` and the ledger page Quota panel agree with
  the slot kinds in [Slots](#slots): the default `codex login` reads
  `interactive` and each `AH_CX_TOKEN_<slug>` account reads `subscription`.
  Like the Claude interactive row, the default login row carries no number and
  sorts after every Codex token row.
  Codex routing left is its week left whenever the week is known. A signed-out
  default login shows `SIGNED_OUT` and a cancelled token `EXPIRED` with `-`
  values. Each Codex row carries its account fingerprint, `#<8 hex>`, after the
  name, and lines under the table name every refused duplicate and whether the
  default login is a distinct ChatGPT account. `balance --current` marks this
  session's account `(current)`. Sessions on
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

### Account identity and own sessions folders

Two credentials can sign in the same ChatGPT account: a token exported under two
slugs from one workspace, or a token account that is also the default `codex
login`. Routing both would count one quota twice. The router compares accounts
by identity, never by token value.

- **Identity.** An account's identity is the `chatgpt_account_id` claim inside
  its credential: the `AH_CX_TOKEN_<slug>` value for a token account, the access
  token in the default login's `auth.json` for `default`. An api slot has none.
  The claim is never printed. Output shows only its fingerprint, the first 8 hex
  characters of its SHA-256, so two rows with the same fingerprint are the same
  ChatGPT account.
- **Duplicates.** When a token account repeats the identity of a token account
  earlier in name order, the later one leaves the routing pool: no automatic
  placement and no swarm seat. It gets a balance row only while live sessions
  still run on it, with `-` for `CAP` and `ROUTING LEFT`. Every
  `agentihooks codex` launch prints one line
  per duplicate on stderr:

  ```text
  agentihooks codex: warning: AH_CX_TOKEN_ncgma_b is the same ChatGPT account as ncgma; it is not routed
  ```

  The fix is to remove one of the two exports from the managed token block, or
  to log the second slug in with another workspace.
- **Default login.** Once any token account exists, `default` leaves the pool
  (see above), so it is never routed beside a token of the same account. The
  balance table still names whether it is the same account as one of them.
- **Own sessions folder.** Codex writes every rollout under
  `CODEX_HOME/sessions`, and every account shares one `CODEX_HOME`. At each Stop
  of a Codex session the hook hard links that session's rollout into
  `CODEX_HOME/accounts/<account>/sessions` at the same relative path, with the
  account taken from the token variable of the Codex process itself. The
  router's `codex exec` quota probe links its thread the same way. A rollout
  outside `CODEX_HOME/sessions` is not linked, and a failed link is logged and
  never blocks the Stop.
- **Reading quota.** An account whose folder exists reads its rate-limit events
  from that folder alone, so one account's reading can never land on another.
  An account with no reading there yet falls back to the session registry's
  attribution, and the default login's fallback skips every session already
  linked into a token account's folder. Sessions that predate the folders keep
  their readings this way until their next Stop links them.

`agentihooks balance` shows the fingerprints and the verdicts:

```text
$ agentihooks balance
#  HARNESS  ACCOUNT           KIND          STATE   SESSIONS  WEIGHT  CAP  ROUTING LEFT  5H LEFT  5H RESET  7D LEFT  7D RESET
1  codex    team_a #5f0e9a71  subscription  NORMAL  0/6       -       6    88%           -        -         88%      4d02h
2  codex    ncgma #ab12cd34   subscription  NORMAL  2/6       -       6    71%           90%      3h10m     71%      2d15h
AH_CX_TOKEN_ncgma_b is the same ChatGPT account as ncgma; it is not routed
codex default login #ab12cd34: same ChatGPT account as ncgma
```

A default login that matches no token account reads
`codex default login #<fingerprint>: distinct from ncgma, team_a`, listing every
token account. A default login whose credential carries no claim, such as one
signed out or signed in with an API key, reads `codex default login: identity
unknown`. With no token account set, no verdict line is printed. `balance
--fable` prints neither the warning nor the verdict.

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
| `QUOTA_USAGE_INJECTION_ENABLED` | `true` | Inject the quota usage banner on every prompt and on the tool call cadence |
| `QUOTA_USAGE_TOOL_CALLS` | `5` | Tool calls between quota usage banners |
| `QUOTA_USAGE_STALE_SEC` | `300` | Age in seconds after which a quota reading is ignored by the banner and the quota policy |

Set them in the shell or in `~/.agentihooks/.env`.

Warning thresholds must be positive and strictly below their corresponding hard
handoff thresholds. For example, setting the weekly hard threshold to `92`
requires a weekly warning threshold below `92`. The swarm rejects invalid
threshold pairs. `QUOTA_POLICY_ENABLED` controls the hook's hard quota policy;
it does not disable the swarm tick's early warnings.
