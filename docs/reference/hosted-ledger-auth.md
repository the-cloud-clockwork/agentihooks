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

## Clients

A workstation swarm, its agents and the ledger hooks reach a hosted ledger by environment alone.

| Variable | Meaning |
|---|---|
| `SWARM_URL` | Address of the hosted ledger, such as `https://swarm.homeofanton.com`. Set, it makes every ledger client remote, as any non-local `AGENTIHOOKS_DEPLOYMENT` already does: the CLI, the swarm tick and the session hook never start or restart a local ledger server. In a session bound to a swarm (`AGENTIHOOKS_SWARM`), it makes only that swarm's ledger remote; a call for another slug is remote only when `LEDGER_URL` or the deployment mode says so, and then still goes to `SWARM_URL`, even when `LEDGER_URL` is set, because `SWARM_URL` wins the address. `LEDGER_URL` remains the fallback; set with no `AGENTIHOOKS_DEPLOYMENT`, it also makes the client remote, and only `AGENTIHOOKS_DEPLOYMENT=local` keeps it local. |
| `AGENTIHOOKS_LEDGER_AGENT_TOKEN` | The agent's own token, sent with its pinned name. |
| `SWARM_SECRET` | When the client holds it and no agent token is set, the client derives the token for its own name. The swarm tick derives each spawned agent's token; neither the secret nor the tick's own agent token reaches a spawned agent. |
| `SWARM_ACCESS_CLIENT_ID`, `SWARM_ACCESS_CLIENT_SECRET` | Cloudflare service token for machines off the tailnet; `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET` are read when these are unset. Both parts set, every API request, upload, event stream, proof ledger call and health probe carries `CF-Access-Client-Id`, `CF-Access-Client-Secret` and `User-Agent: agentihooks-swarm`, because Cloudflare refuses Python's default user agent with error 1010. |
| `LEDGER_CA_PEM` | The PEM of the CA that signed the ledger's certificate. Set, it is the only CA an https ledger request trusts. |
| `LEDGER_CA_FILE` | Without `LEDGER_CA_PEM`, a CA file trusted beside the system roots. Defaults to `ledger-ca.pem` in the agentihooks home, which `agentihooks init` writes from OpenBao `k8s/agentihooks-swarm-ledger-ca`, field `ca.crt`, when `OPENBAO_URL` and `OPENBAO_TOKEN` are set. |

Every reader of `ledger_link.remote` treats `SWARM_URL` as a non-local deployment, so a process with it set never accepts page-derived agent tokens. A herdr pane gets its token from the launcher, which exports it after sourcing the operator's env files and unsets `SWARM_SECRET`.

The tick's own service writes still need `AGENTIHOOKS_CONTROLLER_CREDENTIAL`, checked against the hosted ledger's Redis.

### Which credential a remote client sends

A remote client picks the first that applies:

1. The tick credential `X-Ledger-Tick`, inside a swarm tick that holds the server secret.
2. `X-Controller-Credential` from `AGENTIHOOKS_CONTROLLER_CREDENTIAL`, when the process has no pinned agent name.
3. For a pinned agent, `X-Ledger-Token` with `X-Ledger-Agent`. The token is `AGENTIHOOKS_LEDGER_AGENT_TOKEN`; without it, the client derives one from `SWARM_SECRET`; without either, it asks the ledger for a `hive.` token with the hive credential `AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL` that `agentihooks hive join` writes.

A remote client refuses, before any request:

- a write that needs the operator credential: `a remote ledger client cannot make service writes; the operator credential stays on its host`;
- a process without a pinned agent, which needs both `AGENTIHOOKS_SWARM` and `AGENTIHOOKS_AGENT_NAME`, and with no controller credential: `unauthenticated: a remote ledger client needs a pinned agent identity; the operator credential stays on its host`;
- a pinned agent with no token, no secret and no hive credential: `unauthenticated: a remote ledger client needs AGENTIHOOKS_LEDGER_AGENT_TOKEN or the hive credential AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL from agentihooks hive join`.

A remote read is tried three times, pausing 0.5 and then 1 second, on a connection error or a 5xx answer; a write is sent once. A request times out after 10 seconds. A `403` whose body carries `Missing or wrong ledger credential` ends the command with `unauthenticated: the ledger server refused the credential`; a `401` or `403` to the hive token request ends it with `unauthenticated: the ledger server refused the hive credential: <status>`. Remote writes never queue in the local outbox: an unreachable ledger ends the command with `ledger server not answering on <address>`.

### Workstation swarm on the hosted ledger

Put the settings in `~/.agentihooks/.env`, the env file the tick's systemd units load. Values come from your secret store, never from git:

```bash
SWARM_URL=https://swarm.example.com
# the server's secret; the tick derives agent tokens from it
SWARM_SECRET=<server secret from your secret store>
# off the tailnet only
SWARM_ACCESS_CLIENT_ID=<service token id from your secret store>
SWARM_ACCESS_CLIENT_SECRET=<service token secret from your secret store>
```

Then run the swarm as usual. With `SWARM_URL` set:

- `agentihooks ledger` commands, the ledger session hook and the tick's ledger watchdog talk to the hosted ledger and never start, check or restart a local ledger server, whatever `LEDGER_AUTOSTART` says.
- When the tick holds `SWARM_SECRET`, each agent it spawns gets `AGENTIHOOKS_LEDGER_AGENT_TOKEN` derived for its own name and the swarm's slug; without it, the spawned agent gets no token. `SWARM_SECRET` and the tick's own agent token are removed from the spawned agent's environment.
- The herdr launcher sources the operator's env files in the pane, runs `unset SWARM_SECRET`, then exports the agent's token, so a secret in those files never reaches the agent.
- `take-master` asks for `AGENTIHOOKS_HIVE_ID` only when `AGENTIHOOKS_DEPLOYMENT` is not `local`. A workstation that only sets `SWARM_URL` takes the master seat as before.

A single agent outside the tick, such as a session started by hand, needs its own token instead of the secret:

```bash
export SWARM_URL=https://swarm.example.com
export AGENTIHOOKS_SWARM=my-ledger
export AGENTIHOOKS_AGENT_NAME=engineer@abc123-0001
export AGENTIHOOKS_LEDGER_AGENT_TOKEN="$AGENT_TOKEN"     # derived for my-ledger and this name
```

### Check the connection

The health probe carries the same headers as every client request:

```bash
curl -sS https://swarm.example.com/healthz \
  -H "CF-Access-Client-Id: $SWARM_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $SWARM_ACCESS_CLIENT_SECRET" \
  -H "User-Agent: agentihooks-swarm"
```

The health JSON names the ledger folder the server serves; on the hosted service that folder is `/data`. A read through the CLI as an agent proves the token:

```bash
agentihooks ledger --slug my-ledger --as engineer@abc123-0001 alert list
```

### Limitations

- `SWARM_URL` names one hosted ledger per process. In a session bound to one swarm, a remote call for another swarm's ledger still goes to `SWARM_URL`, even when `LEDGER_URL` is set.
- The service token headers are sent only when both parts are set. One part alone sends neither, so the request reaches Access without a service token.
- The client trusts the system roots for `https`, plus `LEDGER_CA_FILE`, or only `LEDGER_CA_PEM` when that is set.

### Client troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `a remote ledger client needs SWARM_URL, the address of the hosted ledger server` | The process is remote through `AGENTIHOOKS_DEPLOYMENT`, but has neither `SWARM_URL` nor `LEDGER_URL` | Set `SWARM_URL` |
| A local ledger server still starts on the workstation | `SWARM_URL` is missing from the environment the hook or tick runs in, or the session is bound to another swarm | Put `SWARM_URL` in `~/.agentihooks/.env`; check `AGENTIHOOKS_SWARM` names the hosted ledger's slug |
| `unauthenticated: a remote ledger client needs a pinned agent identity` | No `AGENTIHOOKS_AGENT_NAME`, or no `AGENTIHOOKS_SWARM` (a pinned agent needs both), and no controller credential | Run as a named agent, or give the tick `AGENTIHOOKS_CONTROLLER_CREDENTIAL` |
| `unauthenticated: a remote ledger client needs AGENTIHOOKS_LEDGER_AGENT_TOKEN or the hive credential` | A named agent with no token, no `SWARM_SECRET` and no hive join | Set `AGENTIHOOKS_LEDGER_AGENT_TOKEN`, or run the agent from the tick |
| `unauthenticated: the ledger server refused the credential` | The token was derived for another slug or name, or from another secret | Derive it again for the exact slug and agent name from the server's current secret |
| `unauthenticated: the ledger server refused the hive credential: <status>` | The hive credential from `agentihooks hive join` is wrong or no longer known to the ledger | Join the hive again, or set `AGENTIHOOKS_LEDGER_AGENT_TOKEN` |
| A spawned agent has no `SWARM_SECRET` | Intended: the tick and the launcher remove it | Nothing to fix; the agent uses its own `AGENTIHOOKS_LEDGER_AGENT_TOKEN` |

## Evidence

- `tests/swarm_ledger/test_swarm_url_client.py` covers the client: `SWARM_URL` makes the client remote and sets its base address, a remote client without an address names `SWARM_URL`, the access headers need both parts and ride every API request, ledger transport request, upload, proof ledger call, proof control call and health probe, a pinned agent derives its token from the server secret, and `SWARM_URL` never starts a local server. `tests/swarm/test_runtime.py` checks that a spawned agent gets its own token and never the server secret; `tests/swarm/test_ledger_watchdog.py` that the watchdog leaves a remote ledger alone.
- `tests/swarm_ledger/test_edge_auth.py` covers the authority. It refuses a forged assertion, a wrong audience, a wrong issuer, an expired assertion, one without an expiry, an HS256 assertion, a failed certs read, the page credential and a wrong or foreign agent token. It accepts a valid assertion and a server secret agent token.
- `tests/swarm_ledger/test_edge_auth_server.py` runs the real ledger server in edge mode:
  - the v1 API serves an Access operator and a server secret agent;
  - the page server takes an operator write behind Access;
  - both refuse the page credential.
