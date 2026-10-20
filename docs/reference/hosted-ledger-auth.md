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
- **The swarm tick** sends `X-Ledger-Tick` set to `<expires>.<signature>`: `expires` is a Unix time, and `signature` is `HMAC-SHA256(SWARM_SECRET, "ledger-tick:<slug>:<expires>")` in hex (`ledger_authority.tick_token`). The server accepts it as the operator when `expires` lies in the next 900 seconds and the signature matches. A request that carries the header is judged on it alone: a wrong tick credential is refused even when an Access assertion or agent token rides along. The tick reads the secret from `SWARM_SECRET`, or from OpenBao record `k8s/swarm`, field `SWARM_SECRET`, when `OPENBAO_URL` and `OPENBAO_TOKEN` are set, and removes `SWARM_SECRET` from its own environment before it spawns agents.

In edge mode the server tries the credentials in this order and stops at the first that applies: `X-Ledger-Tick`, then `Cf-Access-Jwt-Assertion`, then `X-Ledger-Token` with `X-Ledger-Agent`. A `hive.` token goes to the Redis check; any other token is compared with the derived agent token. The `/api/v1/ledgers/<slug>` routes still accept `X-Controller-Credential` when every one of those is refused.

A refused credential on `/api/v1/ledgers/<slug>` answers `403` with error code `forbidden` and the message `Missing or wrong ledger credential`. The same code with `Host not allowed` means the request's `Host` is missing from `SWARM_PUBLIC_URL` and `SWARM_ALLOWED_HOSTS`.

## Configuration

### Local compose, no public ledger

Leave `SWARM_AUTH_MODE` unset. The page token stays the operator credential and agents derive their tokens from it, as on a workstation. See [Deployment modes](deployment-modes.md#no-public-ledger).

### Hosted server

Put the ledger behind a Cloudflare Access application, then give the server its team, the application audience tag and the server secret:

```bash
SWARM_AUTH_MODE=edge
SWARM_ACCESS_TEAM=<team>                       # or <team>.cloudflareaccess.com
SWARM_ACCESS_AUD=<Access application audience tag>
SWARM_SECRET="$SWARM_SECRET"                   # from your secret store, never committed
```

The audience tag is on the Access application's overview page in the Cloudflare dashboard. It is not a secret; `SWARM_SECRET` is, and anyone holding it can act as every agent and as the tick.

On Anton the antoncore `swarm` chart sets the three non-secret variables in its values. `SWARM_SECRET` is generated once by an External Secrets `Password` generator, pushed to OpenBao record `k8s/swarm` with `updatePolicy: IfNotExists`, and read back into the `swarm-secrets` Kubernetes secret that the pod loads. Rotating it means writing a new value to that OpenBao field. Once the pod runs with the new value, every derived agent token and tick credential changes with it, so running agents need their tokens derived again.

### Workstation agent

A workstation agent that talks to the hosted ledger needs only environment; [Clients](#clients) describes each variable:

```bash
SWARM_URL=https://swarm.example.com
AGENTIHOOKS_LEDGER_AGENT_TOKEN="$AGENT_TOKEN"            # or SWARM_SECRET, to derive it
SWARM_ACCESS_CLIENT_ID="$SWARM_ACCESS_CLIENT_ID"         # off the tailnet only
SWARM_ACCESS_CLIENT_SECRET="$SWARM_ACCESS_CLIENT_SECRET"
```

## Usage

Derive an agent token for one ledger and one name, with the secret in the environment:

```bash
python3 -c 'import hashlib, hmac, os, sys
print(hmac.new(os.environ["SWARM_SECRET"].encode(), f"{sys.argv[1]}\n{sys.argv[2]}".encode(), hashlib.sha256).hexdigest())' \
  my-ledger engineer@abc123-0001
```

or from Python, `ledger_authority.agent_token(os.environ["SWARM_SECRET"], "my-ledger", "engineer@abc123-0001")`. The token is bound to both the slug and the name: it is refused on another ledger and with another `X-Ledger-Agent`.

Read a ledger as that agent:

```bash
curl -sS https://swarm.example.com/api/v1/ledgers/my-ledger \
  -H "X-Ledger-Agent: engineer@abc123-0001" \
  -H "X-Ledger-Token: $AGENT_TOKEN" \
  -H "CF-Access-Client-Id: $SWARM_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $SWARM_ACCESS_CLIENT_SECRET" \
  -H "User-Agent: agentihooks-swarm"
```

Writes made this way carry the agent's name; the server refuses an operation whose `by` names someone else.

The operator opens `https://swarm.example.com/<slug>` in a browser, signs in through Access, and acts as the operator on the page and the API. No ledger token is needed.

## Limitations

- Edge mode accepts only RS256 assertions from the configured team; it does not read Access identity claims, so every identity the Access policy lets through acts as the operator. Restrict the Access policy to the operator.
- The tick credential and agent tokens share `SWARM_SECRET`; there is no per agent revocation short of rotating it.
- The server keeps one key client per certs URL for the life of the process and caches the signing keys it fetched; a certs read times out after 10 seconds.

## Troubleshooting

| Symptom | Cause | Check |
|---|---|---|
| The browser gets `403 forbidden` after the Access login | `SWARM_ACCESS_TEAM` or `SWARM_ACCESS_AUD` is unset or wrong, or the server cannot reach `https://<team domain>/cdn-cgi/access/certs` | Compare the audience tag with the Access application; fetch the certs URL from inside the pod |
| Any client gets `403 forbidden` with `Host not allowed` | The address the client used is not an allowed host | Add the host to `SWARM_ALLOWED_HOSTS` |
| An agent gets `403 forbidden` | Token derived for another slug or name, the server has no `SWARM_SECRET`, or the secret was rotated | Re-derive the token from the current secret with the exact slug and `X-Ledger-Agent` value |
| An agent that worked locally is refused once `SWARM_URL` is set | A page-derived token; remote clients never use it | Set `AGENTIHOOKS_LEDGER_AGENT_TOKEN` or `SWARM_SECRET` |
| A machine client gets Cloudflare error 1010 | Python's default user agent | Send `User-Agent: agentihooks-swarm`; the agentihooks clients already do |
| A machine client gets the Access login page | Missing service token headers | Set `SWARM_ACCESS_CLIENT_ID` and `SWARM_ACCESS_CLIENT_SECRET` |
| The tick's writes are refused | Clock skew beyond the 900 second window, or the tick holds a different secret than the server | Check the host clock and the secret source the tick reads |

## Clients

A workstation swarm, its agents and the ledger hooks reach a hosted ledger by environment alone.

| Variable | Meaning |
|---|---|
| `SWARM_URL` | Address of the hosted ledger, such as `https://swarm.homeofanton.com`. Set, it makes every ledger client remote, as any non-local `AGENTIHOOKS_DEPLOYMENT` already does: the CLI, the swarm tick and the session hook never start or restart a local ledger server. `LEDGER_URL` remains the fallback; set with no `AGENTIHOOKS_DEPLOYMENT`, it also makes the client remote, and only `AGENTIHOOKS_DEPLOYMENT=local` keeps it local. |
| `AGENTIHOOKS_LEDGER_AGENT_TOKEN` | The agent's own token, sent with its pinned name. |
| `SWARM_SECRET` | When the client holds it and no agent token is set, the client derives the token for its own name. The swarm tick derives each spawned agent's token; neither the secret nor the tick's own agent token reaches a spawned agent. |
| `SWARM_ACCESS_CLIENT_ID`, `SWARM_ACCESS_CLIENT_SECRET` | Cloudflare service token for machines off the tailnet. Both set, every API request, upload, event stream, proof ledger call and health probe carries `CF-Access-Client-Id`, `CF-Access-Client-Secret` and `User-Agent: agentihooks-swarm`, because Cloudflare refuses Python's default user agent with error 1010. |
| `LEDGER_CA_PEM` | The PEM of the CA that signed the ledger's certificate. Set, it is the only CA an https ledger request trusts. |
| `LEDGER_CA_FILE` | Without `LEDGER_CA_PEM`, a CA file trusted beside the system roots. Defaults to `ledger-ca.pem` in the agentihooks home, which `agentihooks init` writes from OpenBao `k8s/agentihooks-swarm-ledger-ca`, field `ca.crt`, when `OPENBAO_URL` and `OPENBAO_TOKEN` are set. |

Every reader of `ledger_link.remote` treats `SWARM_URL` as a non-local deployment, so a process with it set never accepts page-derived agent tokens. A herdr pane gets its token from the launcher, which exports it after sourcing the operator's env files and unsets `SWARM_SECRET`.

The tick's own service writes still need `AGENTIHOOKS_CONTROLLER_CREDENTIAL`, checked against the hosted ledger's Redis.

## Evidence

- `tests/swarm_ledger/test_edge_auth.py` covers the authority. It refuses a forged assertion, a wrong audience, a wrong issuer, an expired assertion, one without an expiry, an HS256 assertion, a failed certs read, the page credential and a wrong or foreign agent token. It accepts a valid assertion and a server secret agent token.
- `tests/swarm_ledger/test_edge_auth_server.py` runs the real ledger server in edge mode:
  - the v1 API serves an Access operator and a server secret agent;
  - the page server takes an operator write behind Access;
  - both refuse the page credential.
- `tests/swarm_ledger/test_authority_context.py` covers the tick credential: it accepts a current `X-Ledger-Tick` and refuses one for another ledger, an expired one, one past the 900 second window, a tampered or malformed one, and any tick credential when the server has no `SWARM_SECRET`.
