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
