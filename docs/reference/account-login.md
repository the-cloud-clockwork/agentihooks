---
title: Developer account login
parent: Reference
nav_order: 16
---

# Developer account login

Use the AgentiHooks login commands to register Claude and Codex subscription
accounts for routing. Run interactive logins in your own terminal: Claude's
upstream setup screen displays its credential. Never capture that screen in an
agent transcript or paste credentials into documentation.

## Account names

Use the target account's email as the slug argument. Both logins lowercase it,
replace `@` with `at`, and replace each remaining character outside `a-z` and
`0-9` with `_`. For example, `developer@example.com` becomes
`developeratexample_com`. Empty names, names starting with `_`, and the reserved
names `status`, `help`, `default`, and `api` are refused.

The commands below contain account names only.

## Claude login

```bash
agentihooks claude login developer@example.com
```

`agentihooks claude login [slug] [--openbao | --no-openbao]` runs
`claude setup-token`, captures its subscription token, and stores it under
`AH_CC_TOKEN_<slug>` in the managed shell block. With no slug, an interactive
terminal prompts for one; without a terminal it exits with usage. The browser
chooses the signed-in account, so use a private window for the intended account.
A failed setup or missing token leaves storage unchanged.

Claude login does not set the refresh authority flag. Other
`agentihooks claude` invocations retain their normal routing behavior.

## Master claude ai login

```bash
export AGENTIHOOKS_MASTER_CLAUDE_LOGIN=true
agentihooks claude login --master
```

`AGENTIHOOKS_MASTER_CLAUDE_LOGIN` is one machine wide setting, read through
the hooks configuration (the environment or an `$AGENTIHOOKS_HOME/*.env`
file). It is off by default, and off leaves every launch unchanged, including
swarms on routed quota tokens or gateway API tokens. Turn it on only when the
workers run on quota tokens separate from the master's login. A swarm config
saved with the former per swarm `master_claude_login` field loads without it;
`agentihooks swarm <slug> set` no longer takes `master-claude-login`. A
launch with no other live master, no login in the one home and no recovery
copy adopts the single former per swarm login under
`$AGENTIHOOKS_HOME/master-claude/<slug>/claude` into the home and removes that
swarm's folder. Several such logins refuse the launch naming every path, and
the operator keeps one; a former login still held by a live master refuses
naming its path. With no login to use or adopt, the launch refuses without
building an empty home. Recovery copies under
`secret/agentihooks/master-claude/<slug>` are no longer read.

With it on, the Claude front door master of every swarm, seat
`master@<slug>` (or a master with no seat, which predates seats), launches
and resumes on route `master-login` from one private home,
`$AGENTIHOOKS_HOME/master-login/claude`, copied from its rendered master
profile without the shared credential link. Pool master seats
(`master-2@<slug>` and up) keep their routed quota tokens. A front door
launched from an order on an explicit subscription or API route takes the
login too, so every swarm's front door, the Doctor's included, registers
Remote Control as `master 1 · <slug>`. `CLAUDE_CONFIG_DIR` and
`CLAUDE_SECURESTORAGE_CONFIG_DIR` both point at that home, the session gets
`--settings {"forceLoginMethod":"claudeai"}`, and its environment drops routed
OAuth tokens, API keys, custom endpoints and provider selectors. Settings that
bring back a credential, an `apiKeyHelper` or another login method refuse the
launch, and so does a swarm whose workers carry no quota token. A login account
that is also a worker quota token launches as the declared Claude master account
(`agentihooks balance master-account`), and declares itself when none is
declared; its live master then holds workers on that account at the reserved
share of the week. Another account already declared master refuses the launch,
naming both. The master gets `--remote-control`.
Engineers, CI agents, planners, pool masters and Codex masters never take this route.

`agentihooks claude login --master` takes no slug and runs `claude auth login`
in the first empty slot; `--slot N` logs slot N in again. Up to
`AGENTIHOOKS_MASTER_LOGIN_SLOTS` logins are stored (default 5), slot 1 in
`$AGENTIHOOKS_HOME/master-login/claude` and slot N in
`$AGENTIHOOKS_HOME/master-login-N/claude`, each with its own OpenBao record,
`secret/agentihooks/master-claude-login` and
`secret/agentihooks/master-claude-login-N`. `--list` prints each stored login,
its account, the live master holding it and its five hour and week usage; `--remove N` deletes a free login
and clears its record. A login command refuses while a master or another login
command holds that slot. Native Claude is the only refresher of the login.

One live master holds one login: two masters never share a slot. Launch
preparation takes the free stored login with the most room left (five hour and
week windows from the router cache and its own sessions' quota snapshots), then
checks it with `claude auth status --text`. A valid login ends the chain.

Hive heartbeats advertise `master_login_enabled` separately from the stored
`master_login` account. An enabled hive can accept private recovery with an
empty inventory; the account remains unknown until the verified native or
subscription launch reports it.

When no valid session is available, preparation reserves an unused slot or
renews the slot it already holds. Native `claude auth login` runs in that
isolated home. The master seat receives an inbox directive with its authorization
link and uses the operator browser connector's already signed in claude.ai
session. It must never sign in, sign out or switch accounts. It replies to that
request with exactly `success`, `captcha` or `failure`. Preparation waits for
the matching reply; success also requires the native login process to finish
and the renewed session to pass the live check.

CAPTCHA, browser failure or a timeout ends the native login attempt and releases
only the launching process's slot claim. The master then takes normal
subscription token routing. This fallback has no Remote Control and no forced
claude.ai login; Remote Control requires a native login. Held slots remain
untouched. No step sends email. Browser outcomes are recorded in the slot's
`login-notifications.jsonl` with transport `inbox`, without credentials.

At 98% of the week or 99% of five hours the quota policy hands the master's seat
off. The successor runs the same chain, returning to a valid native login when
one is available.

Each claim takes an epoch above the one stored in OpenBao. With `--openbao`, and
at each master launch and Stop where `OPENBAO_URL` and `OPENBAO_TOKEN` are set,
the latest grant native Claude saved is copied to that slot's record with its
owner epoch, never replacing a newer copy; a launch whose home has no login
restores it from there.

## Codex login

```bash
agentihooks codex login developer@example.com
```

`agentihooks codex login <slug> [--device-auth] [--openbao | --no-openbao]`
runs the browser login, or device login with `--device-auth`, in an isolated
`CODEX_HOME` at `~/.agentihooks/codex/<slug>`. It selects Codex's file credential
store, which saves the token pair in `auth.json`. A successful login writes
`AH_CX_TOKEN_<slug>` and enables `AGENTIHOOKS_AICODE_TOKEN_REFRESH` in the managed
shell block. A failed login does not update that block or OpenBao.

The forms `agentihooks codex login` without a slug, `login status`, `login help`,
and `login --<flag>` pass through to native Codex login instead of registering
a routed account.

## Codex logout

```bash
agentihooks codex logout developer@example.com
```

`agentihooks codex logout <slug> [--openbao | --no-openbao]` removes one Codex
account from every place login registered it: the `AH_CX_TOKEN_<slug>` and
`AH_CX_REFRESH_<slug>` keys of the OpenBao Codex record, the account's
`CODEX_HOME` at `~/.agentihooks/codex/<slug>`, and its export in the managed
shell block. It also removes the unnormalised name a login replaced. OpenBao
runs first with check-and-set, keeping every other account; a failed write
leaves the local registration in place so the command can be repeated. The
OpenBao choice follows the same flags and prompts as login. Other shells keep
the variable until `unset AH_CX_TOKEN_<slug>` or a new shell.

`agentihooks codex logout` without a slug, `logout status`, `logout help` and
`logout --<flag>` pass through to native Codex.

## Duplicate Codex accounts

Two `AH_CX_TOKEN_` names whose tokens carry the same ChatGPT account id are one
account. The router keeps one routed name for it: the slug of the email in the
token's profile claim, else a slug shaped like an email (`<user>at<domain>`),
else the first name in sort order. The other names are reported by
`agentihooks codex` as the same account and are left out of routing, the hive
slots and quota readings the Swarm page Hives table shows, and the swarm quota
table. Remove a duplicate with `agentihooks codex logout <slug>`.

## Managed shell token block

The login commands maintain a block between these marker names:

```text
# ===AGENTIHOOKS-AICODING-TOKENS===
# ===AGENTIHOOKS-AICODING-TOKENS-END===
```

It lives in `~/.zshrc` on macOS and `~/.bashrc` elsewhere. Writes replace matching
exports, add missing exports, preserve unrelated lines and the file mode, and
replace the file atomically. Follow the login's instruction to source that file
in your own shell. Do not print or read the credential file into an agent tool.

The swarm tick, controller, and launcher read account exports from this block
without sourcing the shell file. A block entry overrides an environment entry
of the same name; accounts present only in the environment remain available.

## OpenBao paths and keys

Both logins support `--openbao` to store without a confirmation prompt and
`--no-openbao` to skip storage. Without either flag, a terminal prompts; a
noninteractive invocation skips this step. `OPENBAO_URL` and `OPENBAO_TOKEN`
configure access. An interactive prompt can also accept credentials for that
write only, without persisting those connection credentials.

| Account | KV v2 path on the `secret` mount | Key names |
|---|---|---|
| Claude | `secret/agentihooks/claude-creds` | `AH_CC_TOKEN_<slug>` |
| Codex | `secret/agentihooks/codex-creds` | `AH_CX_TOKEN_<slug>`, `AH_CX_REFRESH_<slug>` |

`AGENTIHOOKS_CODEX_CREDS_PATH` overrides the Codex path relative to the mount;
its default is `agentihooks/codex-creds`. Writes merge account keys with
check-and-set so other accounts remain intact. The Codex refresh key is stored
when the login returns a refresh token. Reports name keys and counts only.
`agentihooks accounts env` reads the credential stores; its exports contain
credentials, so consume them in the intended process rather than displaying them.

OpenBao storage happens after the local shell block is written. A refused or
failed OpenBao write exits nonzero while the local login remains saved.

## One refresh authority

Designate exactly one machine to refresh each Codex account. Codex refresh tokens
rotate on use, so two independent refreshers can invalidate each other's state.
`AGENTIHOOKS_AICODE_TOKEN_REFRESH` defaults to `false`; only `true` enables
`agentihooks codex refresh` and the session-start refresh kick. Codex login sets
it on the login machine. Keep it disabled on other consumers of that account.

`AGENTIHOOKS_AICODE_TOKEN_STORE` selects the refresher's storage:

| Setting | Read and write behavior |
|---|---|
| `local` (default) | Reads local Codex `auth.json` files; saves the new pair locally, updates the shell block, and writes OpenBao when configured. |
| `openbao` | Reads `AH_CX_REFRESH_<slug>` keys and writes rotated pairs to OpenBao; requires `OPENBAO_URL` and `OPENBAO_TOKEN`. Updates the shell block only if it exists, without changing `auth.json`. |

The OpenBao store retains a rotated pair in a private local recovery file if its
remote write fails, then retries persistence on the next run. A refused grant
after a lost rotation response requires a fresh Codex login.

See [Load Balancing](../pillars/load-balancing.md#refreshing-tokens) for refresh
cadence and recovery details, and [Configuration](configuration.md) for the
complete environment variable reference.
