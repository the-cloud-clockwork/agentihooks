---
name: get-current-balance
description: >
  Name the Claude OAuth account (the AH_CC_TOKEN_<slug>) the running session is
  routed to and show the quota table for every account. Use when the operator
  says "get current balance", "get-current-balance", "which account am I on",
  "what balance is this session using", or asks which slug or quota the current
  session runs on.
---

# Get Current Balance

`agentihooks balance --current` owns detection, the probe, and the cache read.
Never read `/proc`, dotenv files, or token variables by hand.

## 1. Run

```bash
agentihooks balance --current
```

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

## 3. Report

Exit 0 means an account was identified. Report the slug, the method, and the
table verbatim. Exit 1 means no account was identified; report the method line
as the answer.
