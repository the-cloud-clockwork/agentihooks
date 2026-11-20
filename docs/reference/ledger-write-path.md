---
title: Ledger write path
parent: Reference
nav_order: 78
---

# Ledger write path
{: .no_toc }

Every ledger write runs the ledger API route code against one shared ledger store. Agents, the tick, the hub, the
controller and every other swarm client run that code in their own process. Only a browser sends writes over HTTP,
and the public host routes them to the ledger API service. The ledger server only reads: it serves the page, the
read API and the live event stream, and answers every write method as unsupported.

There is one write path. No setting switches a client back to the ledger server, and no client falls back to it when
the database is slow. Pull request 11561 delivered it on the operator's order of 16 November 2026.

1. TOC
{:toc}

---

## Who writes and how

| Writer | Path | Judged as |
|---|---|---|
| `agentihooks ledger` commands, `LedgerClient`, the stop hook, proof ledgers, uploads, outbox replays | In process: `scripts.swarm_ledger.ledger.client(slug)`, which returns `scripts.tick.adapters.database_ledger.client` | The pinned agent, or the swarm's own service for service writes |
| The tick pass and the tick ledger port | In process, inside `database_ledger.acting("")` | The swarm's own service |
| The hub admission claim and its release | In process; the hub sets no swarm or agent name | The swarm's own service |
| The controller's hosted ledger | In process, inside `database_ledger.acting("")` | The swarm's own service |
| The ledger page in a browser | HTTP `POST`, `PUT` or `PATCH` to the public host, routed to the ledger API service | The operator's page session or ledger token, as [Hosted ledger auth](hosted-ledger-auth.md) describes |

An in-process client builds a `ResourceClient` whose transport is `database_ledger.request`. Each request becomes an
in-memory exchange handed to the same route functions the ledger API service serves (`routes.ledger_get` for reads,
`routes.ledger_operation` for writes), so revision guards, operation receipts, gates and derivations apply exactly as
they do over HTTP. No socket is opened and no ledger server is contacted. A refusal comes back as the HTTP error the
service would have sent, so callers keep handling `409 revision_conflict`, `404 ledger_missing` and the others the
same way.

### The store a write lands in

The route code writes the store the process is configured for:

- Postgres when `AGENTIHOOKS_SWARM_DATABASE_URL` is set, through `PostgresLedgerRepository`. Every process that
  writes a ledger must carry the same URL; [Shared ledger database sessions](shared-ledger-database.md) covers the
  writer lock, session limits and the writer floor.
- Otherwise the SQLite store in the `LEDGER_DIR` folder. Only processes that see the same folder share it, as the
  services of one Compose stack do through their shared data volume.

Since every writer reaches the store directly, access to the shared database is the write trust boundary. A process
holding the database URL can write any ledger on it.

### Who a call acts as

`database_ledger.principal()` names the caller the route code judges, in this order:

1. The name set by `database_ledger.acting(name)`. The empty name is the swarm's own service.
2. A pinned agent, by its name. A process is pinned when it carries both a swarm and an agent name: a registered
   agent session, or `AGENTIHOOKS_SWARM` with `AGENTIHOOKS_AGENT_NAME`.
3. Otherwise the swarm's own service.

Two refusals protect this:

- When the process acts as a named principal, a pinned agent or an `acting` name, a request that names another
  agent in `X-Ledger-Agent` is refused with `403 forbidden` and `this process acts as <principal>, not <claimed>`.
  A process acting as the swarm's own service admits any `X-Ledger-Agent`.
- A pinned agent on a remote host that asks for a service write without the tick's credential is refused with
  `a remote ledger client cannot make service writes; the operator credential stays on its host`
  (`authority_context.service_refused`). `LedgerClient` raises it as a `SwarmError`, the ledger command exits with it
  and the stop hook raises it as a `PermissionError`.

`X-Ledger-Token` is checked only behind the HTTP service. In process the store access is the credential, so a join
or acknowledgement probe reports only refused and unreachable faults.

### Use it

The ledger command runs the route code in process. Reads and writes look the same as before the change:

```bash
agentihooks api --slug my-swarm get tasks/t-docs
agentihooks api --slug my-swarm --as engineer@host-1 comment tasks/t-docs "The reference page landed"
```

Python code takes the same client. A tick, hub or other service caller wraps its calls in `acting("")`:

```python
from scripts.swarm_ledger import ledger
from scripts.tick.adapters import database_ledger

with database_ledger.acting(""):
    api = ledger.client("my-swarm")
    state = api.snapshot("my-swarm")
```

Never build a `ResourceClient` without `transport=`: the `ledger-client-in-process` rule refuses it (see
[Guards](#guards)).

## Refusals and size warnings

A write is refused only for a reason that belongs to one of its own operations, such as a focus target the ledger
does not hold or an agent writing in another agent's name. The overview and phase description word counts are
warnings about the whole document: every write recomputes them, none of them rejects an operation, and the swarm
ledger client prints them apart from the refusal reason. Pull request 12385 delivered that split on 19 November 2026.

### Where size warnings come from

After a write's operations are applied, the route code runs `ledger_core.warnings(doc)` over the whole ledger:

| Warning | Raised when |
|---|---|
| `overview has <n> words, limit 200` | The overview, counted up to its `Summary` heading (`ledger_close.intro`), has more than 200 words |
| `phase <id> description has <n> words, limit 100` | A phase description has more than 100 words |

Every phase is checked on every write, including phases the write never touched, so an older phase whose
description is over the limit adds its line to the reply of every later write. The route stores the
size lines first and the refusal texts of the write's own operations after them in the reply's `_meta.warnings`,
and each new size line raises one `size` alert for the master.

`ledger_core.size_warning(text)` matches exactly those two shapes. Both clients below use it to tell a warning from
a refusal reason.

### What a refused write prints

`LedgerClient._call` raises `LedgerRefused` when the reply's `rejected` list is not empty. It builds the message from
`_meta.warnings` in stored order:

1. Each size warning prints to standard error as `ledger <slug> warning: <text>`, one line per warning.
2. The other lines are the refusal reasons. They are joined with `; ` into `ledger <slug> refused: <reasons>`.
3. When no refusal reason remains, the message names the rejected operation identifiers instead, printed as a Python
   list: `ledger <slug> refused: ['freeze_set-3f9c2a1b7e']`.

The reply itself is left unchanged, so `_meta.warnings` still holds every line.

Focusing a task the ledger does not hold, on a ledger whose overview and phase `p113` are over their limits:

```bash
agentihooks swarm my-swarm focus tasks/t-missing
```

The command exits with status 1 and writes three lines to standard error:

```text
ledger my-swarm warning: overview has 201 words, limit 200
ledger my-swarm warning: phase p113 description has 117 words, limit 100
swarm: ledger my-swarm refused: freeze_set refused: target tasks/t-missing does not exist
```

Only the last line is the refusal: `freeze_set` refused the focus because `tasks/t-missing` is not on the ledger.
Correct the target, or add the task first, and run the command again. The two warning lines describe the document;
shorten the overview or the named phase description to clear them.

Python callers catch the same error. The size warnings reach standard error before the exception is raised:

```python
from scripts.swarm.ledger_client import LedgerClient, LedgerRefused

try:
    LedgerClient().freeze("my-swarm", "focus", "tasks/t-missing")
except LedgerRefused as exc:
    print(exc)  # ledger my-swarm refused: freeze_set refused: target tasks/t-missing does not exist
```

A reply with an empty `rejected` list is returned unchanged and prints nothing, whatever size warnings it carries.
The warnings stay in `_meta.warnings` and in the master's `size` alerts.

### The ledger command

The write commands of `agentihooks api` and `agentihooks ledger` apply the same rule in `ledger.refused`. They exit
with only the refusal reasons, joined with `; `; size warnings are left out and not printed. A command that reports
its post, such as `say`, prints `{"posted": false}` first. When no refusal reason remains, each rejected operation
the command sent is named instead:

```text
<op> on <item> refused without a reason from the server: check that the entry exists, that you may change it and that its text is not empty
```

### Refusals printed by older releases

Before pull request 12385, `LedgerClient` joined every `_meta.warnings` line into the refusal. The focus example
above then printed a single line that put the size warnings first:

```text
swarm: ledger my-swarm refused: overview has 201 words, limit 200; phase p113 description has 117 words, limit 100; freeze_set refused: target tasks/t-missing does not exist
```

In such a line, or in a log written by an older client, skip every part that matches a size warning shape. The
remaining part is the refusal.

## The ledger API service

`scripts.swarm_ledger.api_service` is the one process that takes ledger writes over HTTP. It serves the ledger page's
writes, runs the schema migration and runs the write side upkeep.

```bash
python -m scripts.swarm_ledger.api_service --serve
```

`--serve` is the only mode; without it the command exits with `--serve is the only mode`.

### Routes it answers

| Method and path | What it does |
|---|---|
| `POST`, `PUT`, `PATCH` under `/api/v1/` | The version one API: operations, saves and setting changes, through `api.handle` |
| `PUT /api/<slug>` | A legacy whole page save |
| `PUT /api/layout` | The viewer's page layout |
| `PUT /api/swarm/<slug>` | A swarm control from the page: start, pause, stop, close, reopen, autonomy, scaling, models and the others the page sends |
| `POST /api/media/<slug>/...`, `POST /api/artifacts/<slug>/...` | A media or artifact upload |
| `POST /api/<slug>/session` | Opens the operator's page session as an `HttpOnly`, `SameSite=Strict` cookie, `Secure` under TLS or edge auth |
| `POST /api/bin` | Moves a ledger to the bin, stopping its swarm, or restores it |
| `GET /healthz` | `200` with `{"dir": "<LEDGER_DIR>"}` |
| `POST /healthz` | Writes the `.healthz` probe file in `LEDGER_DIR`, then answers as `GET /healthz` |
| `OPTIONS` | A CORS preflight for the write routes |

Any other `GET` answers `404` with `the ledger API service takes writes; read from the ledger server`. The service
defines no `DELETE` handler; no ledger write uses that method, so a `DELETE` gets `501` from the HTTP server.

### Settings

| Setting | Default | Meaning |
|---|---|---|
| `LEDGER_API_HOST` | `127.0.0.1` | Address to listen on; containers set `0.0.0.0` |
| `LEDGER_API_PORT` | `8766` | Port to listen on |
| `LEDGER_DIR` | `~/development-ledger` | The ledger folder: the SQLite store without a database URL, plus media, artifacts and the probe file |
| `AGENTIHOOKS_SWARM_DATABASE_URL` | unset | The shared Postgres ledger database |
| `LEDGER_TLS_CERT`, `LEDGER_TLS_KEY` | unset | Serve https with this certificate and key |
| `SWARM_PUBLIC_URL`, `SWARM_ALLOWED_HOSTS` | the ledger server's defaults | The public origin and the `Host` values the service answers; it always adds `localhost` and `127.0.0.1` on its own port |
| `SWARM_AUTH_MODE` | `local` | `edge` marks the page session cookie `Secure` |

The service reads the same Redis and bus settings as the ledger server for the swarm controls and relays it runs.

### Start and stop

At start the service:

1. On the shared database, runs `migrate()`: the schema and the writer floor. Clients never create or change the
   shared schema, so start the API service before any other writer on a new database. On a SQLite folder it takes the
   folder's writer lease instead, without waiting. A second writer on the same folder exits with
   `ledger folder <dir> already has an active writer, pid <pid> on <host>; a second writer is refused`.
2. Adopts legacy ledger files into the store and reloads the operator writes an earlier server recorded as not yet
   relayed to the inbox, so the upkeep loop replays them.
3. Starts the upkeep loop, every two seconds: bin purge on each pass; binning closed ledgers without a swarm and
   alert expiry every fifteen passes; the unrelayed write replay every five passes.
4. Starts the outbox relay to the bus.

It then prints one start line that names the methods it answers, so a method routing rule can be read against it:

```text
ledger API service on http://0.0.0.0:8766 answering GET, POST, PUT, PATCH, OPTIONS, dir /data
```

`SIGTERM` stops the listener, ends the upkeep loop and, on a SQLite folder, releases the writer lease.

## The read only ledger server

The ledger server (`scripts.swarm_ledger.ledger_server`) keeps every read: the page, `GET /api/v1`, the event
stream, media files and exports. Its HTTP handler defines only `GET` and `OPTIONS`:

- `POST`, `PUT`, `PATCH` and `DELETE` get `501 Unsupported method` from the HTTP server. So does `HEAD`, which the
  fronts send to it.
- Its CORS preflight allows only `GET`.
- Its ledger services carry no write hooks: each hook (bin, layout, write gate, inbox relay, alert delivery, swarm
  control and its arguments, lifecycle, quota refresh, the doctor stop phrase) raises `405 method_not_allowed` with
  `The ledger server only reads; the ledger API service takes writes`, and it holds no doctor controls.

The ledger server and the API service build their services through the same factory, `ledger_server.api_services`;
the API service passes every write hook and the ledger server passes none.

## Route page writes by method

Every deployment puts one front on the public address and splits requests by method: `GET` and `HEAD` go to the
ledger server, every other method to the API service.

| Deployment | Front | Reads to | Writes to |
|---|---|---|---|
| Compose stack from `agentihooks swarm init` | nginx service `front` on `${SWARM_BIND:-127.0.0.1}:${SWARM_PORT:-8765}` | `${SWARM_LEDGER_SCHEME:-http}://swarm:8765` | `http://api:8766` |
| Local ledger Compose file `deploy/compose/ledger.yaml` | nginx service `front` on `127.0.0.1:${LEDGER_PORT:-8765}`, host network | `127.0.0.1:${LEDGER_READ_PORT:-18765}` | `127.0.0.1:${LEDGER_API_PORT:-8766}` |
| `agentihooks-swarm` chart | Traefik `IngressRoute` `<fullname>-api` on `api.publicRoute.host` | The existing ledger ingress | `<fullname>-api` port `http` for `POST`, `PUT`, `PATCH`, `DELETE` and `OPTIONS` |

Clients never use the front for writes; only the browser does. A smoke check of the front must read and write
through it, since every other client bypasses it.

### Compose stack

`agentihooks swarm init` copies the stack and starts `swarm` (the ledger server), `api`, `front`, `controller`,
`redis` and `nats`, and waits until `redis`, `nats`, `swarm`, `api` and `controller` report healthy. The `swarm`,
`api` and `controller` services mount the same `swarm-data` volume at `/data`, so without a database URL they share
one SQLite folder, and the `api` service holds its writer lease. With `AGENTIHOOKS_SWARM_DATABASE_URL` in the stack
`.env`, all of them use that database.

```yaml
  api:
    command: [python, -m, scripts.swarm_ledger.api_service, --serve]
    environment:
      LEDGER_API_HOST: 0.0.0.0
      LEDGER_API_PORT: "8766"
      LEDGER_DIR: /data
      SWARM_ALLOWED_HOSTS: api:8766,${SWARM_ALLOWED_HOSTS:-}
    volumes:
      - swarm-data:/data
  front:
    image: mirror.gcr.io/library/nginx:1.27-alpine
    ports:
      - "${SWARM_BIND:-127.0.0.1}:${SWARM_PORT:-8765}:8765"
    volumes:
      - ./deploy/compose/ledger-front.conf:/etc/nginx/templates/default.conf.template:ro
```

The front's routing rule, from `deploy/compose/ledger-front.conf`:

```nginx
set $upstream ${SWARM_LEDGER_SCHEME}://swarm:8765;
if ($request_method !~ ^(GET|HEAD)$) {
    set $upstream http://api:8766;
}
```

The repository root `compose.yaml` has the same services for a source build.

### Local ledger Compose file

`deploy/compose/ledger.yaml` runs the workstation ledger as three host network services: `ledger` on
`LEDGER_READ_PORT` (default 18765), `api` on `LEDGER_API_PORT` (default 8766) and `front` on `LEDGER_PORT` (default
8765), which keeps the address clients and the browser already use. The front renders
`deploy/compose/ledger-local-front.conf.template` with only the three port settings, and waits until `ledger` and
`api` are healthy.

## Helm chart configuration

The chart's `api` block deploys the API service. It renders only when `api.enabled` is true and `hiveOnly` is false.

| Value | Default | Meaning |
|---|---|---|
| `api.enabled` | `false` | Deploy the API Deployment, Service and NetworkPolicy |
| `api.replicas` | `1` | Replica count; at least 1 |
| `api.port` | `8766` | Container and Service port, named `http` |
| `api.env`, `api.secretEnv` | `{}`, `[]` | Settings for the API container only, added after `ledger.env` and `ledger.secretEnv` |
| `api.podDisruptionBudget.enabled`, `.maxUnavailable` | `false`, `1` | A PodDisruptionBudget, rendered only with more than one replica |
| `api.publicRoute.host` | `""` | The public host; when set, the chart adds the Traefik `IngressRoute` for write methods |
| `api.publicRoute.priority` | `1000` | Traefik route priority among IngressRoutes on the host; the route matches only write methods and `OPTIONS`, so reads keep the ledger's Ingress |
| `api.publicRoute.entryPoints` | `[]` | Traefik entry points |
| `api.publicRoute.serversTransportName` | `""` | A Traefik `ServersTransport`, for example one that trusts the ledger certificate |
| `api.publicRoute.tls` | `{}` | The route's `tls` block |
| `api.affinity`, `api.tolerations` | `{}`, `[]` | Pod scheduling |
| `api.resources` | requests cpu `50m`, memory `256Mi`, ephemeral storage `64Mi`; limits memory `1Gi`, ephemeral storage `256Mi` | Container resources |

The API container runs `python -m scripts.swarm_ledger.api_service --serve` with `LEDGER_API_HOST=0.0.0.0`,
`LEDGER_DIR=/data` on an `emptyDir` volume, `SWARM_PUBLIC_URL` and `SWARM_ALLOWED_HOSTS` built from the release, and
readiness and liveness probes on `GET /healthz`. When `ledger.tls.secretName` is set it mounts that secret, serves
https, probes over https and the `IngressRoute` sends to it with `scheme: https`.

An overlay that turns it on:

```yaml
ledger:
  secretEnv:
    - name: AGENTIHOOKS_SWARM_DATABASE_URL
      secretName: agentihooks-swarm-database
      key: url
api:
  enabled: true
  replicas: 2
  podDisruptionBudget:
    enabled: true
  publicRoute:
    host: swarm.example.com
    entryPoints: [websecure]
    serversTransportName: ledger-tls
    tls:
      secretName: swarm-example-com-tls
```

The chart refuses to render:

| Message | Fix |
|---|---|
| `api.enabled needs the shared database: set AGENTIHOOKS_SWARM_DATABASE_URL in ledger.env or ledger.secretEnv, since the api service writes the ledger over it` | Add the database URL to `ledger.secretEnv` (or `ledger.env`). The API Pod's `/data` is an `emptyDir`, so only the shared database keeps its writes |
| `api.replicas must be at least 1` | Set `api.replicas` to 1 or more |

### The database URL every component receives

Every component that writes the ledger runs the route code in process, so it needs the database URL. The chart
copies `AGENTIHOOKS_SWARM_DATABASE_URL` from `ledger.secretEnv` (as a secret reference) or `ledger.env` into the hub,
the controller, `worker` Pods and worker hives. A component that sets the variable in its own `env` or `secretEnv`
keeps its own value. A worker hive with `envFromSecrets` gets none from the chart; put the URL in one of those
secrets.

### Network policy

With `networkPolicy.enabled` and `api.enabled`, the `<fullname>-api` NetworkPolicy admits the release's own Pods and
`networkPolicy.ingressFrom` on the `http` port. Add the ingress controller there so the public route reaches the
API Pods.

### Hosted hub

A hosted hub writes task claims in process, so `agentihooks hub run` refuses to start when the ledger is remote and
no shared database is set. It prints this line and exits 2:

```text
hub: a hosted hub writes task claims in process over the shared database and needs AGENTIHOOKS_SWARM_DATABASE_URL
```

The ledger is remote when `ledger_link.remote()` says so: outside local client mode, with `SWARM_URL` set, or with a
hosted ledger that answers at `AGENTIHOOKS_HOSTED_LEDGER_URL` for an agent or hive ledger credential ([Client mode
resolution](deployment-modes.md#client-mode-resolution)).

## Guards

| Guard | What it holds |
|---|---|
| `tests/swarm_ledger/test_ledger_write_cutover.py` | The hub claim and release land in the store with no ledger server request; a stale hub claim is refused by the route; the ledger server answers `POST`, `PUT`, `PATCH` and `DELETE` with `501`; every write hook of the read only ledger server refuses with `405`; a hosted hub without the shared database refuses to start |
| Feature map entry `swarm-ledger-api-writes` | The behaviour, its operator words and its tests; its Semgrep rule `feature-ledger-api-writes` holds the in-process `client` and the hub's database check |
| Feature rule `feature-tick-database-ledger` | The tick route acts as `database_ledger.acting("")`, the tick ledger port builds its client with `ledger.client`, and `ledger.client` returns the in-process client with no other path |
| Rule `ledger-server-reads-only` | No `do_POST`, `do_PUT`, `do_PATCH` or `do_DELETE` in the ledger server module, whether defined, assigned or set with `setattr`, and no `setattr` under a computed name there |
| Rule `ledger-client-in-process` | No `ResourceClient` anywhere in the tree without an in-process transport, by keyword or position, and none with `transport=None` |
| Incident rule `ledger-write-switch` | No `database_ledger.serving()` or `database_ledger.scoped(...)` under `scripts/` or `hooks/`: no switch between paths |
| Feature map entry `swarm-refusal-size-warnings` | A refused swarm write prints each size warning on its own `ledger <slug> warning:` line and keeps it out of the refusal reason, the missing focus target refusal and the rejected identifier fallback hold, and an accepted reply comes back unchanged with nothing printed. Its tests are `test_a_refused_focus_reports_size_warnings_separately` and `test_size_warnings_do_not_refuse_an_accepted_swarm_write` in `tests/swarm/test_ledger_refusals.py`; `test_the_swarm_client_binds_agent_authored_writes_to_the_session` in `tests/swarm_ledger/test_ledger_authority.py` also checks an authorization refusal and its separate warning line |

`ledger-server-reads-only` and `ledger-client-in-process` run in the whole tree scan from
`.semgrep/ledger-server-writes.yml`, with red and clean fixtures in `.semgrep/ledger-server-writes.test.py`.
`ledger-write-switch` is in `.semgrep-incidents/ledger-server-write.yml` with its fixtures in
`.semgrep-incidents/ledger-server-write.py`.

## What was removed

These no longer exist; an older page or note that names them describes the previous write path:

- The HTTP write transport in the ledger client (`server_call`, `remote_call`), the `talk` queue that waited for a
  down ledger server, and ledger server autostart from a write.
- The ledger server's write routes, its writer lease and its write upkeep.
- The cutover switch: the `ledger_cutover` module, the `api-url` and pending API url swarm settings, the
  `ledger-ready` command and scoped routing.
- Schema creation by clients on the shared database; only the API service migrates it. A SQLite folder still sets up
  its tables on first open.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| A write gets `501 Unsupported method` from the ledger server | The writer runs a release from before the write path change and still posts to the ledger server, as hive Pods on an older image do. Check the release with `agentihooks hive list` and roll the hives onto the current release. A browser seeing it means the front sends writes to the ledger server: check the method routing above |
| The page reads fail with `404` and `the ledger API service takes writes; read from the ledger server` | The front sends `GET` to the API service. Route `GET` and `HEAD` to the ledger server |
| `this process acts as <principal>, not <claimed>` | The request names another agent in `X-Ledger-Agent` than the pinned agent or the `acting` name. Write as the process's own agent |
| `a remote ledger client cannot make service writes; the operator credential stays on its host` | A pinned agent on a remote host asked for a service write. Write as the agent; service writes belong to the tick, hub and controller |
| `agentihooks hub run` exits 2 naming `AGENTIHOOKS_SWARM_DATABASE_URL` | A hosted hub has no shared database. Set the URL on the hub; the chart copies it from `ledger.secretEnv` |
| The API service exits at start with `already has an active writer` | Another process holds the SQLite folder's writer lease. Stop that writer by its process id, or move both onto the shared database |
| A controller, hub or worker gets `404 ledger_missing` (`No such ledger`) for a ledger the page shows | Its process writes a different store: it has no database URL and opens an empty SQLite folder of its own, or the shared schema was never migrated. Give it the shared URL and start the API service against that database first, since only the API service creates the schema |
| `server refused: 500` with `storage_error` and `Resource could not be read or written` | The route code could not read or write the store. Check that the process's database URL or `LEDGER_DIR` names the shared store and that the database answers |
| `ledger <slug> warning: overview has <n> words, limit 200` or `phase <id> description has <n> words, limit 100` printed before a refusal | Expected: these are document size warnings, printed apart from the refusal. Act on the `refused:` line. Shorten the named overview or phase description to clear the warnings; see [Refusals and size warnings](#refusals-and-size-warnings) |
| A refusal names `overview has <n> words, limit 200` or `phase <id> description has <n> words, limit 100` next to another reason | The client runs a release from before pull request 12385, which joined size warnings into the refusal. The other reason is the refusal. Roll the client or hive onto a current release |
| `ledger <slug> refused: ['<op>-<id>']` | The ledger rejected the operation and gave no reason beyond size warnings. Check that the target exists, that the writer may change it and that its text is not empty |
| Compose `swarm init` stops with `api is <state>` | Read `docker compose --project-name agentihooks-swarm logs api`; the API service starts once Redis and NATS are healthy |

## Related pages

- [Service split design record](service-split.md): where the API service sits in the planned service split.
- [Deployment modes](deployment-modes.md): moving a swarm to the hosted ledger and which work runs where.
- [Shared ledger database sessions](shared-ledger-database.md): the writer lock, session limits and the writer floor.
- [Hosted ledger auth](hosted-ledger-auth.md): page and agent credentials behind the HTTP services.
- [Docker Compose quick start](../getting-started/compose-quick-start.md): the stack `swarm init` starts.
