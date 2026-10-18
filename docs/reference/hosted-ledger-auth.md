---
title: Hosted ledger auth
parent: Reference
nav_order: 25
---

# Hosted ledger auth

The ledger server decides who each request acts for in `ledger_authority.caller`. The page server, the `/api/v1` routes, routing settings and proof credentials all go through it. `SWARM_AUTH_MODE` selects the mode.

| Variable | Mode | Meaning |
|---|---|---|
| `SWARM_AUTH_MODE` | both | `edge` for the hosted service. Unset or any other value is `local`. |
| `SWARM_ACCESS_TEAM` | edge | Cloudflare Access team name (`anton`) or team domain (`anton.cloudflareaccess.com`, with or without `https://`). |
| `SWARM_ACCESS_AUD` | edge | The Access application audience tag. |
| `SWARM_SECRET` | edge | Server secret that agent tokens are derived from. Delivered as a secret, never committed. |

## Local mode

Today's behaviour. The token inside each ledger page is the operator credential. An agent's token is the HMAC of that page token over the ledger slug and the agent name, sent as `X-Ledger-Token` with `X-Ledger-Agent`. Hive tokens (`hive.` prefix) are checked against Redis.

## Edge mode

The server never accepts the page token.

- **Operator.** Cloudflare Access adds `Cf-Access-Jwt-Assertion` to every request it lets through. The server fetches the team's signing keys from `https://<team domain>/cdn-cgi/access/certs` and caches them. It accepts the assertion only when all of these hold:
  - it is signed RS256 by one of those keys;
  - it names `SWARM_ACCESS_AUD` as audience and the team domain as issuer;
  - it carries `iat`, and an `exp` that has not passed.

  A browser that passed the Access login therefore reaches the page and the API as the operator. A missing team or audience refuses every assertion, and so does a certs read that fails.
- **Agents.** Two ways in:
  - `X-Ledger-Token` set to `HMAC-SHA256(SWARM_SECRET, "<slug>\n<agent name>")` in hex (`ledger_authority.agent_token(secret, slug, name)`), sent with `X-Ledger-Agent`. A remote client passes it through `AGENTIHOOKS_LEDGER_AGENT_TOKEN`.
  - A hive member's `hive.` token from `POST /api/v1/ledgers/<slug>/agent-token`, which keeps its Redis check.

  Without `SWARM_SECRET`, only hive tokens are accepted.
- **Machine clients outside the tailnet** also need the Cloudflare service token headers to get past Access. Those headers do not make the request the operator; it is the agent token that names who acts.

## Evidence

- `tests/swarm_ledger/test_edge_auth.py` covers the authority. It refuses a forged assertion, a wrong audience, a wrong issuer, an expired assertion, one without an expiry, an HS256 assertion, a failed certs read, the page credential and a wrong or foreign agent token. It accepts a valid assertion and a server secret agent token.
- `tests/swarm_ledger/test_edge_auth_server.py` runs the real ledger server in edge mode:
  - the v1 API serves an Access operator and a server secret agent;
  - the page server takes an operator write behind Access;
  - both refuse the page credential.
