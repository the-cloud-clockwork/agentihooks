---
title: Docker Compose Quick Start
nav_order: 1
parent: Getting Started
permalink: /docs/getting-started/compose-quick-start/
---

# Docker Compose Quick Start

Start the packaged swarm service bundle for **Claude Code and Codex**. It provides Redis, the ledger web service and a controller. Independent agent sessions can use [native installation](installation.md) without these services.

## Prerequisites

Install Python 3.11 or later, Docker with a running daemon, and the Docker Compose plugin. Install AgentiHooks:

```bash
uv tool install agentihooks
```

Agent hosts separately need Claude Code or Codex, authenticated accounts or API credentials, and their configuration bundle. The service containers do not inherit a host's CLI login.

## Start the services

```bash
agentihooks swarm init
```

Initialization copies the packaged Compose files into `$AGENTIHOOKS_HOME/swarm-stack` (default `~/.agentihooks/swarm-stack`). On first use it writes a random Redis password, selects free ports and sets the image tag to `release`. It waits for healthy services and a responsive ledger page, then prints the ledger URL and client setup command. Rerunning preserves the password, ports and data.

## Connect a client

Open the printed ledger URL. In the shell that will use the stack, run the printed client setup command; with the default state directory:

```bash
set -a
. "$HOME/.agentihooks/swarm-stack/client.env"
set +a
```

Use the printed location if `AGENTIHOOKS_HOME` overrides the default. Source this generated file locally; do not paste its credentials into chat or logs.

## Choose standalone or swarm work

A standalone Claude Code or Codex session uses profiles and hooks without a swarm. A swarm adds a shared task ledger, a master and coordinated workers. Initialization creates no swarm and launches no agents. Connect an admitted execution host before starting swarm work; Compose services alone do not provision workers. Use the [swarm guide](../pillars/swarm.md) for creation, host setup and deployment controls.

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
docker compose ps
docker compose logs --tail 50 swarm controller redis
docker compose down
```

Initialization reports missing Docker, daemon or Compose dependencies and unhealthy services with a suggested fix. `down` keeps named volumes; avoid `--volumes` when retaining ledger and Redis data. Keep service ports on loopback for local use; remote host admission and TLS require separate setup.
