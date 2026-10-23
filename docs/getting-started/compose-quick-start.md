---
title: Docker Compose Quick Start
nav_order: 1
parent: Getting Started
permalink: /docs/getting-started/compose-quick-start/
---

# Docker Compose Quick Start

Start the packaged swarm service bundle for **Claude Code and Codex**. It provides Redis, a NATS server with JetStream, the ledger web service and a controller. Independent agent sessions can use [native installation](installation.md) without these services.

## Prerequisites

Install Python 3.12 or later, Docker with a running daemon, and the Docker Compose plugin. Install AgentiHooks:

```bash
uv tool install agentihooks
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
3. On first use writes `.env` there, readable only by you: a random Redis password, a free ledger port (8765 when free), a free Redis port (6379 when free) and `SWARM_IMAGE_TAG=release`. It also writes `client.env`, the settings a shell needs to reach the stack.
4. Runs `docker compose up --detach --wait` for `redis`, `nats` (the event bus, JetStream on the `nats-data` volume, reachable only inside the stack), `swarm` (the ledger server) and `controller`. The first run pulls the released images.
5. Waits until all four services are healthy, up to `--timeout` seconds (default 300), then checks that the ledger's `/healthz` route answers and its home page is served.
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

- Every Compose call on the stack adds `deploy/compose/adopt.yaml`. The `swarm` service runs as the recorded user and group, mounts the ledger folder at its own absolute path and the host `~/.agentihooks` at the same path, and shares the host PID and network namespaces, so server pidfiles stay valid on the host. It runs the ledger server from the recorded checkout with reload on, bound to 127.0.0.1:8765, against the recorded Redis, so every swarm keeps its ledger, inbox items and seats. Stop the legacy ledger container by its id before the bundle `swarm` service starts on that port.
- `agentihooks ledger serve --ensure` and `--stop` for the adopted folder, and the Doctor and watchdog restarts built on them, start and stop the bundle `swarm` service. The legacy `ledger` service of the `agentihooks-ledger` Compose project and the host process server are not started again.
- A shared ledger folder other than the adopted one is refused with `the swarm stack adopts <adopted>, not <folder>; the legacy ledger service stays down`.
- The stack Redis gives the `swarm` user every command except `FLUSHALL` and `FLUSHDB` (`+@all -flushall -flushdb`). Redis answers a flush with a `NOPERM` error, so the bundle cannot empty the shared Redis data it takes over. Hive member users keep `-@dangerous` and the `default` user stays off.

`LEDGER_SERVER_RUNTIME=process` still forces the host process server. Adoption prepares the move only: it does not transfer Redis data, move live swarm seats or activate hives.

To check adoption on a running stack:

```bash
cd "${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/swarm-stack"
grep '^SWARM_ADOPT_\|^SWARM_PORT' .env
project="$(sed -n 's/^SWARM_COMPOSE_PROJECT=//p' .env)"
docker compose --project-name "${project:-agentihooks-swarm}" exec redis grep -c 'flushall -flushdb' /data/users.acl
```

The count is `1` when adoption is recorded. Redis rebuilds the ACL from its template on every start, so a stack started before `--adopt --no-start` picks up the flush deny once `agentihooks swarm init` brings Redis up again with the adoption overlay.

## Diagnose or stop the stack

```bash
cd "${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/swarm-stack"
project="$(sed -n 's/^SWARM_COMPOSE_PROJECT=//p' .env)"
docker compose --project-name "${project:-agentihooks-swarm}" ps
docker compose --project-name "${project:-agentihooks-swarm}" logs --tail 50 swarm controller redis
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
| `controller is <state>` | Read its logs; fix Redis and the ledger server first, since the controller starts after both are healthy |
| `no prepared swarm Redis client to adopt` | Run `agentihooks swarm init --prepare-clients`, then `agentihooks swarm init --adopt` |
| `the ledger page does not answer` | Read the ledger logs; the message names the route that did not answer or did not serve the ledger home page |

`<state>` is the service's health or container state, such as `unhealthy`, `exited` or `created`; `not created` when Compose never made it and `in an unknown state` when `docker compose ps` itself failed. Every fix except the adoption one ends by running `agentihooks swarm init` again. A refused swarm name or creation prints `Swarm not created:` with the reason instead. The `Logs:` command init prints names the Compose project, so it works from any directory. `down` keeps named volumes; avoid `--volumes` when retaining ledger and Redis data. Keep service ports on loopback for local use; remote host admission and TLS require separate setup.

## Share one database across hosts

Set `AGENTIHOOKS_SWARM_DATABASE_URL` to the shared Postgres connection URL and `AGENTIHOOKS_SWARM_REDIS_URL` to the shared Redis connection URL before initialization. Both URLs are retained in the private stack settings and generated client settings. Every ledger server and swarm engine must use the same database and Redis. Without the database setting, the ledger uses SQLite in `LEDGER_DIR`.

Supply credentials through environment settings or your secret manager. Hosted Kubernetes services use ExternalSecret references. Do not print connection URLs or paste generated client settings into chat. Keep TLS certificate verification enabled for remote connections.

Provision the database before activation. Existing SQLite ledgers require one initial conversion with full history and per ledger task, event and artifact counts verified before changing writers. Keep SQLite read only afterward; run no recurring copies or synchronization. Confirm the cutover with one agent write visible within seconds through both the local page and the hosted Anton page, and verify both show the same live tasks and agents.
