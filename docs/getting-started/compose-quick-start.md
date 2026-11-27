---
title: Docker Compose Quick Start
nav_order: 1
parent: Getting Started
permalink: /docs/getting-started/compose-quick-start/
---

# Docker Compose Quick Start

Start the packaged swarm service bundle for **Claude Code and Codex**. It provides Redis, a NATS server with JetStream, the ledger web service and a controller. Independent agent sessions can use [native installation](installation.md) without these services. For the shortest path to a started swarm with this machine as its hive, follow the [single machine quick start](single-machine-quick-start.md); for a cluster, the [Kubernetes quick start](kubernetes-quick-start.md); to join further machines, [add a hive](add-hive-quick-start.md).

## Prerequisites

Install Python 3.12 or later, Docker with a running daemon, and the Docker Compose plugin. Install AgentiHooks:

```bash
uv tool install "agentihooks[nats]"
```

Agent hosts separately need Claude Code or Codex, authenticated accounts or API credentials, and their configuration bundle. The service containers do not inherit a host's CLI login.

## Start the services

```bash
agentihooks swarm init my-swarm --plan ~/plans/my-plan.md --repo ~/dev/my-repo \
  --operator-asked "start a swarm for my plan"
```

`swarm init` takes the swarm name first. The name is one or two lowercase words joined by a dash and ending in `swarm`, such as `my-swarm` or `rig-grade-swarm`; it names both the swarm and its ledger. Unless `--no-start` or `--prepare-clients` is given, a missing or invalid name is refused before Docker is touched, with the rule and two suggestions built from the plan title.

One run does the following, in order:

1. Checks that `docker` is on the `PATH`, that the daemon answers `docker info` and that `docker compose version` works.
2. Copies the Compose files packaged with AgentiHooks into `$AGENTIHOOKS_HOME/swarm-stack` (default `~/.agentihooks/swarm-stack`).
3. On first use writes `.env` there, readable only by you: a random Redis password, a free ledger port (8765 when free), a free Redis port (6379 when free), `SWARM_DEPLOYMENT=compose` and `SWARM_IMAGE_TAG=release`. It also writes `client.env`, the settings a shell needs to reach the stack. The `controller` and `hive` services take their deployment mode only from the stack's own `SWARM_DEPLOYMENT` (default `compose`), while `swarm` and `api` always run `local`, and the base stack services take their scheduler state backend only from `SWARM_SCHEDULER_STATE_BACKEND` (default `bus`), so an `AGENTIHOOKS_DEPLOYMENT` or `AGENTIHOOKS_SCHEDULER_STATE_BACKEND` in the shell that runs Compose never reaches them.
4. Runs `docker compose up --detach --wait` for `redis`, `nats` (the event bus, JetStream on the `nats-data` volume, reachable only inside the stack), `swarm` (the read only ledger server), `api` (the ledger API service, which takes the page's writes), `front` (the nginx front on the ledger port, which sends `GET` and `HEAD` to `swarm` and every other method to `api`) and `controller`. The first run pulls the released images.
5. Waits until `redis`, `nats`, `swarm`, `api` and `controller` are healthy, up to `--timeout` seconds (default 300), then checks that the ledger's `/healthz` route answers and its home page is served. Every client in the stack and on the host writes the ledger in process over the shared store; only the browser's writes pass through `front` to `api` ([Ledger write path](../reference/ledger-write-path.md)).
6. Creates the swarm and its ledger inside the stack, paused. Without `--content`, the ledger holds one automatic phase titled after the `--plan` file, or after the name when there is no plan. Without `--repo`, the swarm works the current directory.

| Option | Use |
|---|---|
| `--plan FILE` | The accepted plan; its first `# ` heading titles the ledger and seeds name suggestions |
| `--content FILE` | Ledger content JSON as `ledger new` takes it, instead of the single automatic phase |
| `--repo DIR` | The repository the swarm works; default the current directory |
| `--template NAME` | The swarm template for caps, models and lanes |
| `--operator-asked WORDS` | The operator's words, required in the shared ledger folder when the content has fewer than three phases or tasks |
| `--timeout SECONDS` | How long to wait for every service to be healthy; default 300 |
| `--no-start` | Check Docker and Compose, write the stack files, `.env` and `client.env`, then stop. No name is needed and no service, swarm or ledger starts |

In the shared ledger folder only a master seat or the operator may create a swarm, and a ledger with fewer than three phases or tasks needs `--operator-asked`. The single automatic phase counts as one, so the example passes it. The quote needs at least three words; run by an agent session it must match words the operator typed to that agent, and run by the operator in a plain shell any three words pass. These refusals come after the stack is up.

### Output

On a terminal the ready line is green, the `Open the ledger` address cyan and a failure red. When output is piped, or `NO_COLOR` is set to a non-empty value, the same text prints without colour. A successful first run prints:

```text
Starting the swarm stack; the first run pulls its images.
Swarm stack ready: redis, nats, swarm and controller are healthy.
Next steps:
  1. Open the ledger: http://127.0.0.1:8765/
  2. Point this shell at the stack: set -a; . /home/me/.agentihooks/swarm-stack/client.env; set +a
  3. Start the swarm only when you want it: agentihooks swarm my-swarm start
Running agentihooks swarm init again is safe: it keeps the password, the ports and the data.
Ledger page: http://127.0.0.1:8765/my-swarm (open it to follow and steer the work)
Swarm my-swarm is paused; start it only when you want it: agentihooks swarm my-swarm start
```

A JSON line describing the new swarm prints before the `Ledger page:` line. The ports in the addresses are the ones written to `.env`.

The stack block prints once the services are healthy and the ledger page answers, before the swarm is created. When creation is then refused, the run ends with a red `Swarm not created:` line and exits 1; the stack stays up and the next run reuses it.

`swarm init` never starts a swarm. The swarm stays paused, and no agent launches, until you run `agentihooks swarm <name> start`.

### Running it again

A second run is safe. It keeps `.env`, so the Redis password, both ports and the data volumes stay the same, and `client.env` keeps pointing at the same addresses. Compose leaves healthy services running. For a swarm that already exists the run prints a JSON line with `"created": false`, its ledger page and the closing `is paused` line, whatever the swarm's state, and changes nothing. A name held by a ledger that is not a swarm's is refused with the rule and suggestions, and so is a name held by a swarm on another repository when `--repo` names a different one.

`SWARM_COMPOSE_PROJECT` in `.env` names the Compose project (default `agentihooks-swarm`). A second stack home under another `AGENTIHOOKS_HOME`, with its own project name, never touches the first.

## Connect a client

Open the printed ledger URL. In the shell that will use the stack, run the printed client setup command; with the default state directory:

```bash
set -a
. "$HOME/.agentihooks/swarm-stack/client.env"
set +a
```

Use the printed location if `AGENTIHOOKS_HOME` overrides the default. Source this generated file locally; do not paste its credentials into chat or logs.

## The ledger front

The browser reaches the ledger only through the `front` service, an nginx 1.27 container published on `${SWARM_BIND}:${SWARM_PORT}` (default `127.0.0.1:8765`). Compose starts it once `swarm` and `api` are both healthy. Its configuration is `deploy/compose/ledger-front.conf` in the stack folder, rendered as an nginx template at every container start:

```nginx
server {
    listen 8765;
    resolver 127.0.0.11 valid=1s;
    client_max_body_size 64m;
    location / {
        set $upstream ${SWARM_LEDGER_SCHEME}://swarm:8765;
        if ($request_method !~ ^(GET|HEAD)$) {
            set $upstream http://api:8766;
        }
        proxy_pass $upstream;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

| Line | What it does |
|---|---|
| `set $upstream ...` and the `if` block | `GET` and `HEAD` go to the read only ledger server `swarm`, every other method to the ledger API service `api` on port 8766. `SWARM_LEDGER_SCHEME` (default `http`) is the scheme the `swarm` service serves |
| `proxy_set_header Host $http_host;` | Each request reaches `swarm` or `api` with the `Host` header the client sent, port included. For a client using a non-default port, nginx's `$host` would drop that port and fail to match the corresponding `host:port` entry |
| `resolver 127.0.0.11 valid=1s;` | `proxy_pass` names a variable, so nginx resolves `swarm` and `api` at request time through Docker's embedded DNS server and caches each answer for one second. Requests after the cached answer expires resolve the name again, allowing nginx to discover a recreated container's new address without restarting `front`. Cache validity does not set a recovery deadline. Without a resolver every request answers `502` |

### Host names the services answer

`swarm` and `api` compare the forwarded `Host` exactly, port included, with their own lists and refuse every other value with `403`:

| Service | `Host` values it answers |
|---|---|
| `swarm` | `swarm:8765`; its own address and loopback names on port 8765 (`0.0.0.0:8765`, `127.0.0.1:8765`, `localhost:8765`); the host and port of `SWARM_PUBLIC_URL`; each `SWARM_ALLOWED_HOSTS` entry |
| `api` | `api:8766`; `127.0.0.1:8765` and `localhost:8765`, the ledger server address it inherits; `127.0.0.1:8766` and `localhost:8766`, its own port; the host and port of `SWARM_PUBLIC_URL`; each `SWARM_ALLOWED_HOSTS` entry |

`SWARM_PUBLIC_URL` defaults to `http://127.0.0.1:${SWARM_PORT}`, the address `swarm init` prints, so the printed page works with no setting. To open the ledger under another name, for example from another machine after setting `SWARM_BIND` to a private interface, set the address in the stack `.env` and run `agentihooks swarm init` again, which recreates both services with it:

```bash
SWARM_PUBLIC_URL=http://swarm.example.internal:8765
SWARM_ALLOWED_HOSTS=swarm.example.internal:8765,ledger.example.internal:8765
```

Both services take both settings. An entry must match the `Host` the browser sends, which carries the port whenever the port is not the scheme's default: `swarm.example.internal` alone does not admit a browser on `swarm.example.internal:8765`.

### The API health check

The `api` health check runs inside the `api` container and requests `http://127.0.0.1:8766/healthz`, so it sends `Host: 127.0.0.1:8766`. The list `api` inherits from the ledger server settings is built from `LEDGER_HOST` and `LEDGER_PORT`, whose port defaults to 8765, so before it listens the service adds `127.0.0.1` and `localhost` on its own port, `LEDGER_API_PORT`. The probe then answers `200` with `{"dir": "/data"}`, `api` turns healthy and `front` starts. With an image built before [pull request 11667](https://github.com/the-cloud-clockwork/agentihooks/pull/11667) the probe gets `403 host not allowed`, `api` stays unhealthy and `front` is never started.

To check the front and the health check on a running stack:

```bash
cd "${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/swarm-stack"
project="$(sed -n 's/^SWARM_COMPOSE_PROJECT=//p' .env)"
port="$(sed -n 's/^SWARM_PORT=//p' .env)"
curl -s "http://127.0.0.1:${port:-8765}/healthz"
docker compose --project-name "${project:-agentihooks-swarm}" exec front grep -E 'resolver|Host' /etc/nginx/conf.d/default.conf
docker compose --project-name "${project:-agentihooks-swarm}" logs api | grep healthz | tail -3
```

The `curl` prints `{"dir": "/data"}` from `swarm` through the front, the `grep` prints the `resolver` and `proxy_set_header Host $http_host;` lines, and the `api` log lines end in `200`.

## Choose standalone or swarm work

A standalone Claude Code or Codex session uses profiles and hooks without a swarm. A swarm adds a shared task ledger, a master and coordinated workers. Initialization creates the paused swarm and its ledger, and launches no agents. Connect an admitted execution host before starting swarm work; Compose services alone do not provision workers. Use the [swarm guide](../pillars/swarm.md) for creation, host setup and deployment controls.

## Adopt an existing workstation ledger

A workstation that already runs the shared ledger folder and Redis outside Docker can hand the ledger server to the bundle without moving its data:

```bash
agentihooks swarm init --prepare-clients
agentihooks swarm init --adopt --no-start
agentihooks swarm init --adopt
```

`--adopt` needs the `swarm-client.env` that `--prepare-clients` writes and refuses without it. It records the current ledger folder (`LEDGER_DIR`, default `~/development-ledger`), the user and group ids, the home directory, this agentihooks checkout, the Redis URL the prepared clients use and the ledger port 8765 (`SWARM_PORT`) in the stack `.env`:

```bash
SWARM_ADOPT_LEDGER_DIR=/home/me/development-ledger
SWARM_ADOPT_HOME=/home/me
SWARM_ADOPT_UID=1000
SWARM_ADOPT_GID=1000
SWARM_ADOPT_CODE=/home/me/agentihooks
SWARM_ADOPT_REDIS_URL=redis://swarm:...@127.0.0.1:6379/0
SWARM_PORT=8765
```

The recording persists: later `agentihooks swarm init` runs without `--adopt` keep it, and each run with `--adopt` writes all seven values again, resetting `SWARM_PORT` to 8765. `--no-start` still checks Docker and the Compose plugin, then writes the stack files, `.env` and `client.env` and starts no service, swarm or ledger; use it to check the recorded values first. `--adopt` and `--prepare-clients` cannot be combined.

While `SWARM_ADOPT_LEDGER_DIR` is set:

- Every Compose call on the stack adds `deploy/compose/adopt.yaml`. The `swarm` service runs as the recorded user and group, mounts the ledger folder at its own absolute path and the host `~/.agentihooks` at the same path, and shares the host PID and network namespaces, so server pidfiles stay valid on the host. It runs the ledger server under its reload supervisor from the recorded checkout with reload on, so a code reload hands the listening socket to a successor while the container keeps running, bound to 127.0.0.1:8765, against the recorded Redis, so every swarm keeps its ledger, inbox items and seats. Stop the legacy ledger container by its id before the bundle `swarm` service starts on that port.
- The adopted `swarm` service sets `AGENTIHOOKS_BUS=memory` and `AGENTIHOOKS_SCHEDULER_STATE_BACKEND=redis` as fixed values, so a host shell or `.env` setting cannot change them. The base stack defaults to `AGENTIHOOKS_BUS=nats` at `nats://nats:4222`, set only through the stack's own `SWARM_BUS` and `SWARM_BUS_URL` so an agent shell's `AGENTIHOOKS_BUS*` values never reach it, and keeps scheduler state on that bus through `SWARM_SCHEDULER_STATE_BACKEND`, default `bus`, but on the host network with `depends_on` reset no Compose `nats` service is reachable and the name `nats` does not resolve. The memory bus matches the workstation tick and inbox on the host, and scheduler state stays in the recorded Redis instead of a store local to one server process. The adopted server therefore neither relays ledger events to NATS nor consumes them; reach a NATS hub only from a stack without adoption or from a hive.
- `agentihooks ledger serve --ensure` and `--stop` for the adopted folder, and the Doctor and watchdog restarts built on them, start and stop the bundle `swarm` service. The legacy `ledger` service of the `agentihooks-ledger` Compose project and the host process server are not started again.
- A shared ledger folder other than the adopted one is refused with `the swarm stack adopts <adopted>, not <folder>; the legacy ledger service stays down`.
- The stack Redis gives the `swarm` user every command except `FLUSHALL` and `FLUSHDB` (`+@all -flushall -flushdb`). Redis answers a flush with a `NOPERM` error, so the bundle cannot empty the shared Redis data it takes over. Hive member users keep `-@dangerous` and the `default` user stays off.

`LEDGER_SERVER_RUNTIME=process` still forces the host process server. Adoption prepares the move only: it does not transfer Redis data, move live swarm seats or activate hives.

To check adoption on a running stack:

```bash
cd "${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/swarm-stack"
grep '^SWARM_ADOPT_\|^SWARM_PORT' .env
project="$(sed -n 's/^SWARM_COMPOSE_PROJECT=//p' .env)"
docker compose --project-name "${project:-agentihooks-swarm}" exec swarm sh -c 'echo "$AGENTIHOOKS_BUS $AGENTIHOOKS_SCHEDULER_STATE_BACKEND"'
docker compose --project-name "${project:-agentihooks-swarm}" exec redis grep -c 'flushall -flushdb' /data/users.acl
```

The `swarm` service prints `memory redis` and the count is `1` when adoption is recorded. Redis rebuilds the ACL from its template on every start, so a stack started before `--adopt --no-start` picks up the flush deny once `agentihooks swarm init` brings Redis up again with the adoption overlay.

If the adopted `swarm` logs keep repeating NATS connect tracebacks, the stack files predate the overlay's memory bus. Run `agentihooks swarm init` from a current agentihooks install: it copies the bundled overlay over the stack files and recreates the changed `swarm` service. Then run the check above and confirm it prints `memory redis`.

## Workstation tailnet fronts

The adopted workstation stack can expose its ledger and Redis fronts through Tailscale. The overlay, nginx template and startup script ship with the stack under `deploy/compose`. They require the existing hive certificate and key under the adopted user's `.agentihooks/hive-tls` folder. The ledger forwards to loopback port 8765 and Redis to loopback port 6379, preserving the workstation's adopted Redis service.

Start or restart the two fronts with:

```bash
bash "${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/swarm-stack/deploy/compose/start-tailnet.sh"
```

Every invocation reads `tailscale ip -4`, refuses a missing or invalid tailnet address recreates only the two fronts and removes the retired hive join front. It overrides any old `SWARM_TAILNET_IP` value without rewriting the private settings. The nginx listener and advertised Redis address both use that address. Run this command after reconnecting the workstation to the tailnet; `docker restart` alone retains the container's previous address. `SWARM_STACK_HOME` selects another stack folder and `SWARM_COMPOSE_PROJECT` selects another project. Arguments such as `config --format json` replace the default startup action.

Hives join through the hosted join endpoint on the Anton Redis, set per hive with the chart's `workers.hiveUrl`. The workstation no longer serves hive joins on the tailnet, so no hive can mint a member on the workstation Redis.

## Diagnose or stop the stack

```bash
cd "${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/swarm-stack"
project="$(sed -n 's/^SWARM_COMPOSE_PROJECT=//p' .env)"
docker compose --project-name "${project:-agentihooks-swarm}" ps
docker compose --project-name "${project:-agentihooks-swarm}" logs --tail 50 swarm api front controller redis
docker compose --project-name "${project:-agentihooks-swarm}" down
```

The packaged `compose.yaml` declares no project name, so pass `--project-name`; a plain `docker compose` in that folder looks for a project named `swarm-stack` and finds nothing.

A failed run exits 1 and names the dependency that failed, its fix and, for a service, the command that shows its logs. When a service fails, the last ten lines Docker Compose printed come first:

```text
docker compose said:
  Error response from daemon: ... bind: address already in use
Swarm stack not ready: redis is created.
  Fix: read its logs; if port 6379 is in use, set SWARM_REDIS_PORT in /home/me/.agentihooks/swarm-stack/.env to a free port, and keep the password in /home/me/.agentihooks/swarm-stack/.env as it was at the first start, then run agentihooks swarm init again.
  Logs: docker compose --project-name agentihooks-swarm logs redis
```

| Message after `Swarm stack not ready:` | Fix |
|---|---|
| `Docker is not installed` | Install Docker Engine with the Compose plugin |
| `the Docker daemon is not reachable` | Start Docker, or add your user to the `docker` group |
| `the Docker Compose plugin is missing` | Install Docker Compose v2, the `docker compose` plugin |
| `redis is <state>` | Read its logs; if the Redis port is in use, set `SWARM_REDIS_PORT` in `.env` to a free port and keep the original password |
| `swarm is <state>` | Read its logs; the ledger server needs Redis healthy, and if the ledger port is in use, set `SWARM_PORT` in `.env` to a free port. With adoption recorded (see [Adopt an existing workstation ledger](#adopt-an-existing-workstation-ledger)), stop the legacy ledger container by its id first |
| `api is <state>` | Read its logs; the ledger API service starts once Redis and NATS are healthy, so fix those first. On a SQLite stack it holds the data folder's writer lease and exits when another writer holds it. Log lines `"GET /healthz HTTP/1.1" 403` mean the image predates the API health check host fix; see [The API health check](#the-api-health-check) |
| `controller is <state>` | Read its logs; fix Redis and the ledger server first, since the controller starts after both are healthy |
| `no prepared swarm Redis client to adopt` | Run `agentihooks swarm init --prepare-clients`, then `agentihooks swarm init --adopt` |
| `the ledger page does not answer` | Read the ledger logs; the message names the route that did not answer or did not serve the ledger home page |

Requests through the ledger port can fail after the services are healthy:

| Symptom | Cause and fix |
|---|---|
| `swarm init` stops with `api is unhealthy` and the `api` logs show `"GET /healthz HTTP/1.1" 403` | The `api` image refuses its own health check `Host`, `127.0.0.1:8766`, because it was built before the fix. Keep `SWARM_IMAGE_TAG=release` in `.env`, or set a tag built after it, and run `agentihooks swarm init` again; `front` starts once `api` is healthy |
| A request through the ledger port answers `403 host not allowed`, or under `/api/v1/` answers `403` with `forbidden` and `Host not allowed` | The `Host` the browser sent is in neither service's list. Open the printed address, or set `SWARM_PUBLIC_URL` or `SWARM_ALLOWED_HOSTS` as [Host names the services answer](#host-names-the-services-answer) shows. When the address is listed and every request is still refused, the stack's front config forwards `$host` and drops the port: run `agentihooks swarm init` from a current install, which copies the current front config, then restart the front |
| The ledger port answers `502` and the `front` logs show `no resolver defined to resolve swarm` or `api` | The stack's front config has no `resolver` line. Run `agentihooks swarm init` from a current install, then restart the front |

`swarm init` copies every packaged stack file on each run, but Compose recreates a container only when its own settings change, and nginx renders its template only when the container starts. After a front config change, restart the front:

```bash
docker compose --project-name "${project:-agentihooks-swarm}" restart front
```

`<state>` is the service's health or container state, such as `unhealthy`, `exited` or `created`; `not created` when Compose never made it and `in an unknown state` when `docker compose ps` itself failed. Every fix except the adoption one ends by running `agentihooks swarm init` again. A refused swarm name or creation prints `Swarm not created:` with the reason instead. The `Logs:` command init prints names the Compose project, so it works from any directory. `down` keeps named volumes; avoid `--volumes` when retaining ledger and Redis data. Keep service ports on loopback for local use; remote host admission and TLS require separate setup.

## Share one database across hosts

Set `AGENTIHOOKS_SWARM_DATABASE_URL` to the shared Postgres connection URL and `AGENTIHOOKS_SWARM_REDIS_URL` to the shared Redis connection URL before initialization. Both URLs are retained in the private stack settings and generated client settings. Every ledger server, ledger API service and swarm engine must use the same database and Redis. Without the database setting, the ledger uses SQLite in `LEDGER_DIR`. The ledger API service creates the shared schema at start, so start it before any other writer on a new database.

Supply credentials through environment settings or your secret manager. Hosted Kubernetes services use ExternalSecret references. Do not print connection URLs or paste generated client settings into chat. Keep TLS certificate verification enabled for remote connections.

Provision the database before activation. Existing SQLite ledgers require one initial conversion with full history and per ledger task, event and artifact counts verified before changing writers. Keep SQLite read only afterward; run no recurring copies or synchronization. Confirm the cutover with one agent write visible within seconds through both the local page and the hosted Anton page, and verify both show the same live tasks and agents.
