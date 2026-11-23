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
the database is slow. Pull request 11561 delivered it on the operator's order of 16 November 2026. A host may run one
warm ledger writer that runs the same route code for every client on that host; see
[The warm writer socket](#the-warm-writer-socket).

1. TOC
{:toc}

---

## Who writes and how

| Writer | Path | Judged as |
|---|---|---|
| `agentihooks ledger` commands, `LedgerClient`, the stop hook, proof ledgers, uploads, outbox replays | This host's warm writer socket first, else in process: `scripts.swarm_ledger.ledger.client(slug)`, which returns `scripts.tick.adapters.database_ledger.client` | The pinned agent, or the swarm's own service for service writes |
| The tick pass and the tick ledger port | In process, inside `database_ledger.acting("")`, even when a writer listens | The swarm's own service |
| The hub admission claim and its release | In process; the hub sets no swarm or agent name | The swarm's own service |
| The controller's hosted ledger | In process, inside `database_ledger.acting("")` | The swarm's own service |
| The ledger page in a browser | HTTP `POST`, `PUT` or `PATCH` to the public host, routed to the ledger API service | The operator's page session or ledger token, as [Hosted ledger auth](hosted-ledger-auth.md) describes |

The in-process transport is `database_ledger.request`. Each request becomes an in-memory exchange handed to the same
route functions the ledger API service serves (`routes.ledger_get` for reads, `routes.ledger_operation` for writes),
so revision guards, operation receipts, gates and derivations apply exactly as they do over HTTP. No ledger server is
contacted. A refusal comes back as the HTTP error the service would have sent, so callers keep handling
`409 revision_conflict`, `404 ledger_missing` and the others the same way.

### The warm writer socket

`database_ledger.client(base, timeout)` builds a `ResourceClient` whose transport is `database_ledger.routed` bound to
the client timeout. Each request tries this host's warm writer first, the `agentihooks ledger writer serve` process on
`ledger-writer.sock` in the agentihooks home, which answers with the same route code as the caller it names:

| Case | What the client does |
|---|---|
| The writer answers | Returns its reply; a status of 400 or more raises the same HTTP error the in-process transport raises |
| No socket file, a refused connection, or any other connect error | Runs the request in process through `database_ledger.request`. Nothing has been sent, so nothing runs twice |
| Inside a tick pass | Runs the request in process, so every tick ledger call keeps its `swarm_tick_ledger_call` journal line and the tick never waits on the writer |
| The writer took the request and the reply is lost: it closed with no answer, the read timed out, or the status line is unreadable | Raises `TimeoutError` and never runs the request in process, because the writer may already have applied it. Callers see the error a timed out ledger request raised before: `ledger.call` exits with it and `LedgerClient` treats the ledger as unanswered |

`database_ledger.request` itself never opens the socket, so the controller's hosted ledger and the writer always
answer in process.

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

### How a hub claim is written

A hub claim is guarded, applied and written on the claimed task's own stored rows. It never reads the whole ledger
and never builds the whole written state, so its cost follows the size of one task, not the size of the ledger.

Before this change every claim ran the whole ledger write. On 23 November 2026 seven claim shaped writes on the
busiest swarm, a ledger of about 39 MB, spent 1.8 to 3.0 seconds of a fresh process loading the whole state for the
guard and 0.35 to 0.81 seconds applying the operation to it under the ledger lock. With the admission snapshot's own
whole ledger reads, hub admissions answered in 8.1 to 22.2 seconds against their eight second budget
([Launch failure limits](../pillars/hives.md#launch-failure-limits)).

#### The claim operation

`Admission._mark` claims through `LedgerClient.update_task`, guarded on the state and the owner the offer saw:

```python
live = ledger.update_task(
    slug,
    task_id,
    {"state": "claimed", "claimed_by": name, "workspace": workspace},
    if_state=("open",),
    if_match={"claimed_by": row.get("claimed_by", ""), "claim_generation": generation},
)
```

The hub sets `state` to `pr` instead when the task already has a pull request, and clears `branch` when the task
names a protected branch. The client sends one operation by the swarm's own service:

```json
{
  "ops": [
    {
      "op": "task_update",
      "id": "claim",
      "by": "swarm",
      "item": "tasks/t1",
      "fields": {"state": "claimed", "claimed_by": "engineer@host-1", "workspace": "~/.agentihooks/swarm/my-swarm/tasks/t1"},
      "if_state": ["open"],
      "if_match": {"claimed_by": "", "claim_generation": 0}
    }
  ]
}
```

A request may also carry `"guards": {"tasks/t1": "<revision>"}` and an `operation_id`; the claim checks them the
same way the whole write does, and a replayed `operation_id` answers from its stored receipt.

#### Which writes take the narrow write

`mutations.apply` hands a request to the repository's `apply_task_update` before any whole ledger read when all of
these hold:

- The request is one `task_update` operation on one `tasks/<id>` item, by the swarm, with no other changes.
- It carries `if_state` or `if_match`.
- It sets only the claim fields `state`, `claimed_by`, `workspace` and `branch`.

`apply_task_update` returns nothing and writes nothing when it cannot prove the narrow write stores what the whole
write would. The request then takes the whole ledger write, `apply_ops`, unchanged. That happens when:

- The task is `blocked` or `done`.
- The ledger's metadata has no `priorities_dismissed` record yet; the whole write creates it.

Every other write, including a claim that sets any other field, takes the whole ledger write as before.

#### What the narrow write does

Under the ledger lock, on the task's own stored rows and the metadata rows it needs, `apply_task_update`:

1. Checks the request's revision guard, the operation receipt, the controller epoch fence and the write gate, then
   `if_state` and `if_match` against the stored row.
2. Holds the claim when the task has an open dependency, and refuses it when the task has a committed outcome.
3. Applies the fields, raises `claim_generation` by one when `claimed_by` changes, stamps each changed field with the
   time, the new revision and `swarm`, and records a `task claimed` event at the new revision.
4. Writes only the rows that changed, the event, the one task's outbox row, the change record and the ledger's
   revision and summary.
5. Returns the touched row, the new revision, this write's events and the rejected operations.

The stored result is the same as the whole write's: for every case the tests cover, the `fields`, `resources`,
`threads`, `events`, `outbox`, `row_changes` and `ledgers` rows and the read back ledger match a whole write of the
same operation on a twin ledger. `PostgresLedgerRepository` inherits the method from `SQLiteLedgerRepository`, so the
shared database runs the same write.

#### What the hub gets back

| Case | Status | Reply | Stored |
|---|---|---|---|
| The claim applies | `200` | `applied` names the operation, `rejected` is empty, `tasks` holds the claimed row with `claim_generation` one higher | The claim fields, their stamps and a `task claimed` event |
| A guard misses: the task is no longer `open`, or `claimed_by` or `claim_generation` moved | `200` | `rejected` is empty, `tasks` holds the row as stored | Nothing changes |
| The task has an open dependency | `200` | `tasks` holds the row, still `open` and unclaimed | Nothing changes on the task |
| The task has a committed outcome | `200` | `rejected` names the operation, `applied` is empty | An open alert on `tasks/<id>` at the reply's revision: `committed outcomes require controller reconciliation` |

The reply carries only these keys, with the row projected the way the read API projects it:

```json
{
  "tasks": [{"id": "t1", "state": "claimed", "claimed_by": "engineer@host-1", "claim_generation": 1}],
  "applied": ["claim"],
  "rejected": [],
  "_meta": {"rev": 9, "warnings": []}
}
```

On a ledger of 1,000 tasks the reply stays under 4,000 characters. `LedgerClient.update_task` picks the row by id
from `tasks`, and `LedgerClient` raises `LedgerRefused` when `rejected` is not empty. The hub keeps its existing
check: when the returned row's `claimed_by` is not the agent it admitted, `Admission._mark` releases the claim and
the agent and admits nothing.

#### After write hooks and publishing

`relay_to_inbox`, `doctor_phrase` and `deliver_alerts` still run after a narrow write, in that order. Each gets a
small state: `tasks` holds only the touched row, `_meta` carries the new revision and the write's events, and
`alerts` holds the alerts to deliver, including one the write raised.

`Publishing.apply_task_update` passes the write through. It reads the stored ledger once to queue a stream
publication only when the read only ledger server has already published that ledger and the narrow write applied;
otherwise it reads nothing.

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

### Host check

Every request is refused unless its `Host` header equals an allowed value exactly, port included. The service starts
from the ledger server's list, `ledger_link.allowed_hosts()`: `LEDGER_HOST`, `127.0.0.1` and `localhost` on
`LEDGER_PORT` (default 8765, and always 8765 for the shared ledger folder), the host and port of `SWARM_PUBLIC_URL`
and each comma separated `SWARM_ALLOWED_HOSTS` entry. That list names the ledger server's port, so before it listens
`serve()` adds `127.0.0.1` and `localhost` on `LEDGER_API_PORT`. A health probe sent to the service's own loopback
port, such as the Compose health check's `http://127.0.0.1:8766/healthz`, is answered with `200`.

With the Compose `api` settings the list is `api:8766`, `127.0.0.1:8765`, `localhost:8765`, `127.0.0.1:8766` and
`localhost:8766`, plus the `SWARM_PUBLIC_URL` host and port and any `SWARM_ALLOWED_HOSTS` entries. Before
[pull request 11667](https://github.com/the-cloud-clockwork/agentihooks/pull/11667) the two `8766` loopback entries
were missing, so the Compose health check got `403`, `api` never turned healthy and `front` never started.

A refused request gets `403` with the text `host not allowed`; under `/api/v1/` it gets `403` with error code
`forbidden` and the message `Host not allowed`.

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

The front's routing, from `deploy/compose/ledger-front.conf`:

```nginx
resolver 127.0.0.11 valid=1s;
location / {
    set $upstream ${SWARM_LEDGER_SCHEME}://swarm:8765;
    if ($request_method !~ ^(GET|HEAD)$) {
        set $upstream http://api:8766;
    }
    proxy_pass $upstream;
    proxy_set_header Host $http_host;
}
```

- `proxy_set_header Host $http_host` forwards the `Host` the client sent, port included. Both services compare it
  exactly with their allowed values ([Host check](#host-check)). For a client using a non-default port, nginx's
  `$host` drops that port and fails to match the corresponding `host:port` entry.
- `proxy_pass` names a variable, so nginx resolves `swarm` and `api` at request time. The `resolver` line sends those
  lookups to Docker's embedded DNS server and caches each answer for one second. Requests after cache expiry resolve
  the name again, allowing nginx to discover a recreated container's new address without restarting the front.
  Cache validity does not set a recovery deadline. Without the resolver every request answers `502` and the front logs
  `no resolver defined to resolve swarm`.

The packaged stack, `scripts/swarm/stack/deploy/compose/ledger-front.conf`, is the same file byte for byte, and the
repository root `compose.yaml` has the same services for a source build. The host names a browser may use are listed
in the [Compose quick start](../getting-started/compose-quick-start.md#host-names-the-services-answer).

### Local ledger Compose file

`deploy/compose/ledger.yaml` runs the workstation ledger as three host network services: `ledger` on
`LEDGER_READ_PORT` (default 18765), `api` on `LEDGER_API_PORT` (default 8766) and `front` on `LEDGER_PORT` (default
8765), which keeps the address clients and the browser already use. The front renders
`deploy/compose/ledger-local-front.conf.template` with only the three port settings, and waits until `ledger` and
`api` are healthy.

This front forwards `Host $http_host` as well, so the services see `127.0.0.1:8765` when the browser uses the
default address. For the shared ledger folder the ledger server's own address is port 8765 whatever its listen port,
so both services admit that value. Its upstreams are loopback addresses rather than names, so it needs no
`resolver` line.

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
| `tests/swarm_ledger/test_task_update_narrow_write.py` with its cases in `tests/swarm_ledger/narrow_write_cases.py` | A hub claim through the API bumps the claim generation, stamps its fields and records `task claimed`; a guard miss leaves the task as it was; an open dependency keeps the task open; relay, doctor and alerts run at the claim's revision; a refused claim raises its alert; the reply is the projected row and the new revision; a claim on a ledger of 1,000 tasks never calls `current`, `get_document`, `_entry` or `Entry.for_write` and answers under 4,000 characters; the narrow write stores the same rows as the whole write in every case, with a guard and on replay; a blocked or done task, or a ledger with no `priorities_dismissed` record, falls back with nothing written; publishing reads the ledger only when it is published and the write applied |
| `tests/swarm_ledger/test_postgres_repository.py` narrow claim tests | The same storage match and fallback on the shared database; they run where `LEDGER_TEST_POSTGRES_URL` is set |
| Feature map entry `ledger-task-update-narrow-write` | The narrow claim write's behaviour and its 14 tests, including `tests/swarm/test_ledger_client_task_guard.py`, where `update_task` returns the claimed row from a reply that holds only that row |
| Feature map entry `swarm-ledger-api-writes` | The behaviour, its operator words and its tests; its Semgrep rule `feature-ledger-api-writes` holds `client` to the warm writer first transport, `partial(routed, timeout)`, and the hub's database check |
| `tests/tick/test_database_ledger_writer_route.py` | A request goes to a listening writer; an absent or refusing socket runs it in process; a lost reply after send raises `TimeoutError` with the route code never called; a writer refusal raises the in-process HTTP error; a tick pass stays in process and journals while a writer listens |
| Feature map entry `compose-api-health-and-front-proxy` | The API service answers a health probe sent to its own loopback port (`test_the_api_service_answers_a_health_probe_sent_to_its_own_loopback_port`); every front config forwards `Host $http_host` and never `$host` (`test_the_front_forwards_the_host_the_client_sent_with_its_port`); both Compose front configs resolve `swarm` and `api` through `127.0.0.11` (`test_the_front_resolves_the_swarm_and_api_names_through_the_compose_dns_at_request_time`) |
| Feature rule `feature-tick-database-ledger` | The tick route acts as `database_ledger.acting("")`, the tick ledger port builds its client with `ledger.client`, and `ledger.client` returns the in-process client with no other path |
| Rule `ledger-server-reads-only` | No `do_POST`, `do_PUT`, `do_PATCH` or `do_DELETE` in the ledger server module, whether defined, assigned or set with `setattr`, and no `setattr` under a computed name there |
| Rule `ledger-client-in-process` | No `ResourceClient` anywhere in the tree without a transport, by keyword or position, and none with `transport=None`; the warm writer first transport of `database_ledger.client` is the one transport beside the in-process one |
| Incident rule `ledger-write-switch` | No `database_ledger.serving()` or `database_ledger.scoped(...)` under `scripts/` or `hooks/`: no switch to the ledger server |

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
| A hub claim still spends seconds in the `guard_read` write step ([Ledger request metrics](ledger-request-metrics.md)) | The claim took the whole ledger write: it sets a field outside `state`, `claimed_by`, `workspace` and `branch`, the task is `blocked` or `done`, or the ledger has no `priorities_dismissed` record yet. The first whole write creates that record. A hub on a release from before the narrow write always takes the whole write: roll it onto the current release |
| The claim write answers `200` but the hub admits nothing | A guard missed: another agent claimed the task, or its claim generation moved after the offer. The reply holds the row as stored, its `claimed_by` is not the admitted agent, and `Admission._mark` released the claim. Nothing to fix |
| The API service exits at start with `already has an active writer` | Another process holds the SQLite folder's writer lease. Stop that writer by its process id, or move both onto the shared database |
| A controller, hub or worker gets `404 ledger_missing` (`No such ledger`) for a ledger the page shows | Its process writes a different store: it has no database URL and opens an empty SQLite folder of its own, or the shared schema was never migrated. Give it the shared URL and start the API service against that database first, since only the API service creates the schema |
| `server refused: 500` with `storage_error` and `Resource could not be read or written` | The route code could not read or write the store. Check that the process's database URL or `LEDGER_DIR` names the shared store and that the database answers |
| Compose `swarm init` stops with `api is <state>` | Read `docker compose --project-name agentihooks-swarm logs api`; the API service starts once Redis and NATS are healthy. Log lines `"GET /healthz HTTP/1.1" 403` mean the image predates the [Host check](#host-check) entries for the service's own port: run an image built after pull request 11667 |
| Every request through a front answers `403 host not allowed`, or under `/api/v1/` `403 forbidden` with `Host not allowed` | The `Host` the client sent is not listed: add the `host:port` to `SWARM_ALLOWED_HOSTS` or set `SWARM_PUBLIC_URL`. When the address is listed, the front forwards `$host` and drops the port; forward `$http_host` |
| A Compose front answers `502` and logs `no resolver defined to resolve swarm` | The front config has no `resolver` line, so nginx cannot resolve the variable upstream. Add `resolver 127.0.0.11 valid=1s;` and restart the front |

## Related pages

- [Service split design record](service-split.md): where the API service sits in the planned service split.
- [Deployment modes](deployment-modes.md): moving a swarm to the hosted ledger and which work runs where.
- [Shared ledger database sessions](shared-ledger-database.md): the writer lock, session limits and the writer floor.
- [Hosted ledger auth](hosted-ledger-auth.md): page and agent credentials behind the HTTP services.
- [Docker Compose quick start](../getting-started/compose-quick-start.md): the stack `swarm init` starts.
