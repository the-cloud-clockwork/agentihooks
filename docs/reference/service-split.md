---
title: Service split design record
parent: Reference
nav_order: 62
---

# Service split design record

This record maps the ledger server and the hub into four services and one agent channel: the ledger frontend, the
API, the app server and the auth service, with an agent WebSocket on the app server. For each ledger route, rule,
side effect and auth path it names today's owner, the target service, the strangler step that moves it and the
rollback of that step. It also sets resource budgets per service and the Docker Compose developer build.

Operator constraints this record follows (swarm culture, 19 and 29 October):

- The swarm services (tick, scheduler, app server, API, auth service and ledger frontend) run in Anton. The
  workstation is a headed hive; it runs agents, the master session and the herdr client, never a swarm service.
- A Docker Compose developer build is the only other target.
- There is one database and one Redis. Every service and every ledger page reads and writes the same Postgres and
  Redis, chosen by environment settings. No copy, sync or import between ledger stores.

The 29 October inventory the plan names was never written down; the master confirmed it was kept only in a session.
The inventory below was mapped from `dev` at `65249ec02` by reading the code named in each table.

## Target in one picture

```
 browser ─── frontend (static HTML, CSS, ES modules) ──┐
                                                         │ reads: GET /api/v1/...   (API)
                                                         │ writes: POST operations   (app server)
                                                         │ stream: GET events (SSE)  (app server)
 agent / hive ── WebSocket /ws/agents ── app server ─────┤
                                         (tick, rules,   │
                                          reactors,      ├── API ── Postgres   (the only Postgres client)
                                          side effects)  │
                                                         └── Redis, NATS (unchanged ownership)
 every caller ── auth service: issues and verifies identity; the other services verify its tokens locally
```

| Service | Owns | Never does |
|---|---|---|
| Frontend | Static pages, styles, scripts, logo; the browser login redirect | Data access, rules, tokens embedded in HTML |
| API | The `LedgerRepository` over Postgres: row reads, guarded op application, revisions, operation receipts, events and the outbox table, media and artifact files | Refusals by identity or swarm state, derivations, Redis reads, side effects, SSE fan-out |
| App server | The tick, hub reactors, the rule engine (gates, refusals, derivations), every side effect, the page event stream and the agent WebSocket | Direct Postgres access |
| Auth service | Caller identity for every request: operator, agent, tick, controller, hive member; credential issue, exchange and revocation | Ledger data, rules |

## Contracts

### API

- Transport: HTTP JSON under `/api/v1`, served by its own process and deployment. The only process holding
  `AGENTIHOOKS_SWARM_DATABASE_URL`.
- Reads: `ledgers`, `ledgers/{slug}`, collection and item paths through `resources.read`, `hierarchy/*`, `tasks`
  pages, done-row paging, `export`, events since a cursor, media and artifact files.
- Writes: one route, `POST ledgers/{slug}/operations`, that applies already-ruled ops. It keeps the two guards that
  are data integrity, not policy, and that exist today in `mutations.GuardedOperations.check`:
  - every changed resource carries an expected revision (`428 revision_required`, `409 revision_conflict`);
  - an `operation_id` replays its stored results and refuses different content (`409 operation_conflict`).
- Op application is `ledger_core.apply_op` and `apply_changes` reduced to their data mechanics: shape validation
  (`check_body`, `check_op` without the text filter) and row changes. The commit writes rows, events and the outbox
  in one transaction, as `sqlite._write` does today.
- Callers: the app server with a service token for every write; the frontend with the operator's token for reads.
  Agents never call the API directly once the WebSocket step lands.
- Import boundary: the API package imports no module from the rule chain, `scripts.gates`, `scripts.swarm` or
  `scripts.hub`. Task da2 pins this with an import graph test.

### App server

- Grows out of the hub (`agentihooks hub run`), task dp1. It keeps every hub reactor and takes the tick from the
  controller deployment.
- `POST ledgers/{slug}/operations` for the page and agents: authenticate, run the rule chain on a fresh read of the
  affected resources, compute derivations, then commit through the API with the revisions it read. A
  `409 revision_conflict` reruns the chain on a fresh read, at most three times, then answers `409` to the caller.
- `GET ledgers/{slug}/events` as server-sent events for the page, built from outbox events on the bus. The
  in-process `events.hub.Hub` moves here unchanged; replicas share nothing, so the client reconnects with
  `Last-Event-ID` and a cursor from another replica answers `410 cursor_expired`, as it does today across restarts.
- Swarm controls: `swarm/controls`, `swarm/actions`, `lifecycle/actions`, `controller-lease`, proof ledgers, routing
  settings, hives, monitor.
- Consumes outbox events for every side effect (task da3), idempotent by event id.

### Auth service

- Verifies every credential that `ledger_authority.caller`, `ledger_authority.controller` and `hive/auth` verify
  today, and answers with a short lived signed token naming the principal (operator `""`, agent name, `tick`,
  `controller`, `service:<name>`), the ledger slug and an expiry.
- The other services verify that token locally against the auth service's published key; no per request call.
- Owns issue and exchange: agent tokens for hive members (`agent-token`), controller and service credentials
  (`issue_controller`, `issue_service`), tick tokens, hive member Redis ACL users and NATS user claims.
- Keeps `SWARM_SECRET`, the controller digest and the NATS signer. Only the auth service mounts them.

### Agent WebSocket

- Endpoint `/ws/agents` on the app server. The agent opens it with an auth token scoped to its swarm, seat and task.
- Frames are JSON with a `type`: `inbox.item`, `inbox.reply`, `inbox.close`, `ledger.op` (the same body as the
  operations route), `ledger.event` (events for the agent's own task and phase), `wake`, `heartbeat`, `ack`.
- Delivery is at least once with stable item ids, as the Redis inbox is today; the agent acknowledges each item.
- Fallback: when the socket is down the client uses today's paths (HTTP operations and the Redis inbox) until the
  WebSocket step is complete for every harness.
- There is no WebSocket server in the code today. The only WebSocket code is the Codex app-server client in
  `codex_session.protocol`; `websockets>=15.0` is already a dependency.

## Route map

Today every route below is served by one process, `ledger_server.Handler`, on port 8765, behind the single ingress.

| Route group (today) | Handler today | Kind | Target |
|---|---|---|---|
| `GET /`, `GET /<slug>`, `/static/*`, `/logo.png`, `/favicon.ico` | `index_page`, `page_for`, `send_static`, `send_logo` | Page and static | Frontend. The page token baked into `shell.html` goes away; the page gets its token from the auth login |
| `GET /healthz`, `POST /healthz` | inline, `probe_write` | Health | Each service serves its own `/healthz`; the write probe moves to the API |
| `GET /media/*`, `/artifacts/*`, `GET ledgers/{slug}/files/*` | `send_media` | File read | API |
| `POST /api/media/*`, `/api/artifacts/*`, `POST uploads/*` | `post_media`, `post_artifact` | File write | App server rules (`ledger_artifacts.refusal`, authorship), stored through the API |
| `GET ledgers`, `bin`, `ledgers/{slug}` and collection reads, `hierarchy/*`, `tasks`, `metadata`, `export` | `global_resource`, `resources.read`, `task_list.read`, `routes.export` | Read | API. The Redis swarm summary that `home_summaries` merges in is added by the app server or fetched by the page from `swarm` |
| `POST operations`, `PUT saves`, legacy `PUT /api/<slug>` | `mutations.apply`, `put_ledger` | Write | App server, which commits through the API. Legacy `PUT /api/<slug>` is removed (the page uses only `/api/v1`) |
| `GET ledgers/{slug}/events` (SSE) | `routes.events` → `events.stream.serve` | Stream | App server |
| `GET swarm`, `swarm/*`, `swarm/models`, `swarm/context/*`, `tasks/{id}/workspace`, legacy `GET /api/swarm/<slug>` | `resources.swarm_read`, `context_view.expand`, `workspace_tails` | Read, Redis | App server |
| `PUT swarm/controls`, `POST swarm/actions`, legacy `PUT /api/swarm/<slug>` | `put_swarm`, `admin.control` | Swarm command | App server |
| `POST lifecycle/actions`, `GET`/`POST controller-lease` | `admin.lifecycle`, `controller_lease` | Command, lease | App server |
| `GET`/`PATCH routing/settings`, `GET`/`PATCH hives/*`, `GET monitor` | `routing.settings`, `hives.route`, `monitor.route` | Read and write, Redis | App server |
| `POST proof-ledgers/*` | `proofs.route` | Command | App server |
| `GET`/`POST migrations/{slug}` | `storage_migration.hosted.route` | Import and capture | API, operator principal only |
| `GET`/`POST architecture`, `architecture/actions` | `architecture.view`, `architecture.act` | File read and write | App server |
| `GET`/`PUT layout`, `GET tips` | `admin.layout`, `page_tips.values` | Page settings | API (layout is per viewer data); tips move into frontend static files |
| `POST bin/actions`, legacy `POST /api/bin` | `admin.bin_action` | Write and swarm stop | App server (it also stops a running swarm) |
| `POST ledgers/{slug}/agent-token` | `routes.agent_token` | Credential exchange | Auth service |
| `OPTIONS *` | `do_OPTIONS` | CORS | Each service |

## Rule map

Every rule below runs today inside the ledger server write path, and most of them inside the database write
transaction: `repository.apply_ops` takes the gate chain and calls `repository.mutation.apply`, which imports
`ledger_alerts`, `ledger_plans`, `ledger_priorities` and `ledger_notifications`. That is why the API cannot be split
off by moving routes alone; the chain has to leave the transaction first.

| Rule | Decides | Target |
|---|---|---|
| `ledger_authority.refusal`, `claim_refusal` | Writing as someone else, unauthored ops, join roles, `to` on comments, profile up claims | App server, fed by the auth principal |
| `ledger_comments.check` and the condition screen (`hooks.filters.check.screen`) | Agent text: ids, paths, hashes, dashes, word limits | App server |
| `ledger_kinds.check`, `check_workflow`, `unmet` | Task kinds, contracts, proof needed for done | App server |
| `ledger_tasks` refusals (`add_refusal`, `rank_refusal`, `update_refusal`, `pinned_refusal`, `park_refusal`, slice and parent refusals) | Who adds, ranks and updates tasks | App server |
| `ledger_rank.apply`, `urgent_refusal` | Urgent on phase held or frozen tasks | App server |
| `ledger_plans.task_refusal`, `drop_refused` | Tasks against the plan; atomic plan writes | App server |
| `ledger_freezes.check`, `author` | Freeze and focus authorship, autonomy from Redis | App server |
| `ledger_verdict.check`, `ledger_close.check`, `ledger_relay` (verified operator words) | Operator only verdicts, close shape, relay quotes | App server |
| `ledger_task_duplicates.Gate` with the `ledger_duplicates` child process | Duplicate tasks, follow ups and phases | App server |
| `master_fence.Fence`, `front_door.Door`, `ledger_blockers.Gate`, `gates.talk.Budget` | Master ownership, front door chat, blocker protocol, talk budget | App server |
| `worker_writes.check`, `GuardedOperations.check_controller` | Stale worker claims, controller epoch fence | App server |
| `ledger_alerts.recovered`, `raise_warning`, `derive`; `ledger_priorities.derive`; `ledger_notifications.derive` | Derived alerts, Priorities and notifications | App server, computed before the commit and sent as ops in the same guarded write |
| `ledger_artifacts.sweep` (file deletes) | Orphan artifact files | App server decides, API deletes after commit |
| Revision guards and operation receipts in `GuardedOperations.check` | Lost updates and replays | API (data integrity) |
| `ledger_core.check_body`, `check_op` shape checks | Malformed ops | API (and repeated in the app server for early refusal) |
| `scripts.gates` tool call gates (`identity`, `watch`, `quiet`, `claim_stop`, others) | Agent tool calls | Unchanged: client side hooks |
| Tick passes (claims, rank, preempt, placement, freezes, reopen, sizing, grouping) | Swarm decisions | App server, where the tick moves |

## Side effect map

| Side effect | Runs today | Target |
|---|---|---|
| Operator write relay to inbox and chat receipts (`relay_to_inbox` → `operator_mail.relay`, `chat_receipts.post`) | After commit, on the request thread | App server outbox consumer (da3) |
| Alert delivery (`deliver_alerts` → `ledger_alerts.deliver`) | After commit, on the request thread | App server outbox consumer (da3) |
| Masters channel decision posts (`master_fence` `publish`) | After commit | App server outbox consumer (da3) |
| Doctor stop phrase (`doctor_phrase` → `swarm_control`) | After commit | App server outbox consumer (da3) |
| Swarm commands (`commands.submit`, terminate, lifecycle, quota refresh) | In `put_swarm`, `admin.control` | App server |
| Unrelayed replay, alert expiry, bin tidy, stream sampling (`watch_ledgers`) | Ledger server background loop | App server timers |
| Outbox relay to the bus (`outbox_host.running`) | Ledger server thread, every second | API process, the only reader of the outbox table |
| Page stream snapshots (`Publishing`, `publish_snapshot`, `ledger_view`) | Ledger server thread | App server |
| Cross host refresh (`hub.projector`, `follow_shared`) | Ledger server | Retired once the app server is the only writer path |

## Auth path map

| Caller | Credential today | Verified today by | Target |
|---|---|---|---|
| Operator, local mode | Per ledger page token in `X-Ledger-Token` | `ledger_authority.principal` | Auth service login for the Compose build; no token in HTML |
| Operator, edge mode | Cloudflare Access JWT | `ledger_authority.access_operator` | Auth service verifies the Access JWT and issues the session token |
| Agent | HMAC of `SWARM_SECRET` (edge) or of the page token (local) with `X-Ledger-Agent` | `ledger_authority.caller` | Auth service; agents hold a scoped token, never a secret derivation |
| Agent on a hive | `hive.<member>.<rand>` token from the `agent-token` exchange | `hive/auth.agent_member` | Auth service issues and verifies |
| Tick | `X-Ledger-Tick` HMAC with expiry | `tick_principal` | Retired: the tick runs inside the app server and uses its service token |
| Controller, hub replicas | `X-Controller-Credential` | `hive/auth.controller`, service digests in Redis | Auth service service tokens |
| Hive member Redis and NATS | Redis ACL user, NATS user claims | Redis ACL, NATS signer | Auth service mints; Redis and NATS keep verifying |
| GitHub webhook | `X-Hub-Signature-256` | `hub.github_webhooks.verify` | Unchanged, its own receiver |
| Every request | Host and Origin allowlist | `routes.dispatch`, `Handler.refused` | Every service, from the same settings |

## Order, strangler steps and rollback

Each step is one or more pull requests into `dev` and keeps today's pages and agents working. Each switch is an
environment setting in the chart values, so rollback is a values change through git and a rollout, never a hand
edit. A step is done when its proof is green and the old path has carried no traffic for one full day, read from
the ledger request metrics.

| Step | Phase | Change | Switch | Rollback |
|---|---|---|---|---|
| 1 | One API owns the database (da2) | Split `repository.mutation.apply` into data application (API) and the rule chain with derivations (ledger server). Stand up the API deployment. The ledger server reaches storage through an HTTP repository that implements `LedgerRepository` against the API, with revision guards on every write | Ledger server repository backend: direct or API | Set the backend to direct and scale the API to zero. Both paths write the same Postgres, so no data moves |
| 2 | One API owns the database (da3) | Relay, alerts, masters channel and doctor stop run from outbox events, consumed by the hub, idempotent by event id | One setting per side effect: in request or outbox | Set it back to in request. Inbox ids are stable, so an effect that ran on both paths during the switch lands once |
| 3 | App server is the control plane (dp1) | The hub becomes the app server: it takes the rule chain and the operations, controls and swarm read routes. The ledger server forwards those routes to it | Ledger server forwards writes: off or on | Turn forwarding off; the ledger server still holds the chain until step 7 |
| 4 | App server is the control plane | The tick moves from the controller deployment and the workstation scheduler into the app server | Controller tick: on or off; app server tick: on or off, never both (the tick lease already enforces one holder) | Turn the controller tick back on; the lease hands over within one lease period |
| 5 | Authentication is its own service | Auth service issues tokens; every service accepts both today's credentials and auth tokens | Accept legacy credentials: on or off | Turn legacy credentials back on. Revoking auth tokens is safe because legacy paths still verify |
| 6 | Agents reach the app server over WebSocket | Agents and hives connect to `/ws/agents`; HTTP operations and the Redis inbox stay as fallback | Client transport: socket or legacy | Set the client transport to legacy. Inbox ids are stable across both |
| 7 | Ledger page is frontend only | Pages served by the frontend; ingress paths split by service; legacy routes and the page token removed; the ledger server process retired | Ingress routes per path | Point the ingress back at the ledger server deployment, which stays deployable at zero replicas until the step is done |

The phase "Take the ledger off the hot path" precedes step 1 and needs no service change. "Agents understand the
ledger" follows step 6 and changes no boundary.

## Resource budgets

Today's figures come from the chart values on `dev`. The target figures are starting budgets; each step's proof
replaces them with the measured peak of a busy swarm hour (`kubectl top` and the ledger request metrics) plus half
again.

| Service | Today | Target requests (cpu / memory) | Target limit (memory) | Replicas | Postgres connections |
|---|---|---|---|---|---|
| Frontend | Part of the ledger StatefulSet: 50m / 512Mi, limit 2Gi | 10m / 32Mi | 128Mi | 2 | 0 |
| API | Part of the ledger StatefulSet | 100m / 256Mi | 1Gi | 2 | Pool of 10 per replica; the only client |
| App server | Hub: 50m / 128Mi, limit 512Mi, 2 replicas; controller: 50m / 128Mi, limit 512Mi | 200m / 512Mi | 2Gi (takes the page views and the stream from the ledger) | 2, singleton reactors and the tick behind leases | 0 |
| Auth service | Inside the ledger server and the controller | 20m / 64Mi | 256Mi | 2 | 0 |
| WebSocket | Not present | Inside the app server; budget 2 MB per connected agent | Counted in the app server limit | Same as app server | 0 |

Redis and NATS keep their current budgets (Redis 50m / 128Mi, limit 512Mi; NATS from its chart).

## Compose developer build

The Compose file at the repository root keeps one image, built from the root `Dockerfile`, and runs each service as
its own container with a different command. It gains a Postgres service, so the developer build uses the same
repository backend as Anton.

| Service | Command | Port | Depends on |
|---|---|---|---|
| `postgres` | Postgres image, data volume | 5432, not published | none |
| `redis`, `nats` | As today | As today | none |
| `api` | API serve command | 8766, internal | postgres |
| `auth` | Auth serve command | 8768, internal | redis |
| `app` | App server run command (hub reactors, tick, rules, stream, WebSocket) | 8767, internal | api, auth, redis, nats |
| `frontend` | Static file server with path routing to `api` and `app` | `${SWARM_PORT:-8765}`, published on `${SWARM_BIND:-127.0.0.1}` | api, app, auth |

- One published port keeps today's address for the browser and the `agentihooks ledger` client.
- `AGENTIHOOKS_SWARM_DATABASE_URL` is set only on `api`, as the chart's `ledger.secretEnv` does today.
- The command names are created by the tasks of each step; until a step lands, its service runs today's ledger
  server or hub command.

## How this was mapped

- Routes: `ledger_server.Handler.do_GET`, `do_POST`, `do_PUT`, `do_PATCH` and `api.routes.dispatch`.
- Rules: `ledger_server.write_gate`, `repository.mutation.apply`, `ledger_core.apply_op` and its extension ops,
  `api.mutations.GuardedOperations`.
- Side effects: `ledger_server.relay_to_inbox`, `deliver_alerts`, `doctor_phrase`, `watch_ledgers`,
  `outbox_host.running`.
- Auth: `ledger_authority.caller`, `controller`, `tick_principal`, `hive.auth`, and the
  [Hosted ledger auth](hosted-ledger-auth.md) page.
- Deployment: the `agentihooks-swarm` chart templates and values, the root `compose.yaml`, and the
  [Deployment modes](deployment-modes.md) page.

Related: [Presentation layer architecture](presentation-layer.md), [Shared ledger database
sessions](shared-ledger-database.md).
