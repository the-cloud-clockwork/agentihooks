---
name: get-agents-quota
description: >
  Show the quota left for every configured agent harness (each Claude account
  and Codex) and name the Claude account the running session is routed to. Use
  when the operator says "get agents quota", "get-agents-quota", "get current
  balance", "which account am I on", "how much Codex quota is left", "what
  balance is this session using", or asks which agent has quota left.
---

# Get Agents Quota

`agentihooks quota` and `agentihooks balance --current` own detection, the
probe, the cache read and the Codex session-log read. Never read `/proc`,
dotenv files, Codex session logs, or token variables by hand.

## 1. Run

```bash
agentihooks quota
agentihooks balance --current
```

`agentihooks quota` prints one row per agent account:
`AGENT ACCOUNT STATE SESSIONS 5H LEFT 7D LEFT 7D RESET SOURCE`. Claude rows come
from the router cache or a live probe (`--refresh`). Codex has one row per account:
`default` (the `codex login` on this machine, `SIGNED_OUT` when logged out) and
each `AH_CX_TOKEN_<slug>`. Its quota comes from the newest rate-limit event in
that account's Codex session logs, so `SOURCE` says how old it is, or
`no session log` when the account has not run yet.
Codex plans that report only a weekly window show `?` under `5H LEFT`.
`--json` prints the same rows as JSON.

Add `--fable` when this session runs a Fable model. Add `--refresh` to skip the
60-second cache for the current account.

## 2. Read the output

The first line is `current=<slug> method=<method>`:

| method | Meaning |
|---|---|
| `oauth-token` | The `CLAUDE_CODE_OAUTH_TOKEN` of the parent Claude process equals `AH_CC_TOKEN_<slug>`. Exact. |
| `sole-token` | No OAuth token is visible; the one `AH_CC_TOKEN_*` that `agenti` left in the session environment names the account. |
| `oauth-token-unmatched` | The session runs an OAuth token that matches no `AH_CC_TOKEN_*`. |
| `unrouted` | The session was not launched by `agenti` and runs on the stored Claude login. |

The table marks the current row `(current)` and probes it live. Other rows come
from the router cache, and `AGE` gives how old each one is. A routed session
holds only its own token, so those rows refresh only when `agenti` or
`agentihooks balance` runs from a shell that exports every `AH_CC_TOKEN_*`.
`SESSIONS n/cap` counts the live Claude sessions on each account right now
(cap: the account's band from its five hour and week windows); sessions on no account are listed
under the table as `unrouted`.

## 3. Report

Report the `agentihooks quota` table verbatim, then the slug and method from
`balance --current`. For that command, exit 0 means an account was identified. Exit 1 means no account was identified; report the method line
as the answer.
