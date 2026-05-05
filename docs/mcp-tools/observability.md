---
title: Observability
nav_order: 10
parent: MCP Tools
---

# Observability Tools
{: .no_toc }

The Observability category provides session log diagnostics and container log tailing across Docker, Kubernetes, and AWS ECS.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## Tools

| Tool | Description |
|------|-------------|
| `read_session_logs()` | Read hooks log entries filtered by session, level, or event |
| `tail_container_logs()` | Tail logs from Docker, Kubernetes, or ECS |

---

## Tool reference

### `read_session_logs`

```python
read_session_logs(
    session_id: str = "",
    level: str = "",
    event: str = "",
    tail: int = 100
) -> str
```

Reads the hooks log (`~/.agentihooks/logs/hooks.log`) and returns matching entries. Every hook event is recorded here: MCP failures, secrets warnings, context refresh, retry breaker trips, file read cache blocks, token warnings, and more.

- **`session_id`** — filter by session (partial match, e.g. `"e361d38d"`)
- **`level`** — filter by keyword in message or payload (e.g. `"error"`, `"warning"`, `"failed"`, `"blocked"`)
- **`event`** — filter by event type in message (e.g. `"Pre tool use"`, `"context_refresh"`)
- **`tail`** — return the most recent N matching entries (default: 100)

**Returns:** JSON with `count`, `total_matches`, `entries` (list of log entry dicts)

---

### `tail_container_logs`

```python
tail_container_logs(
    runtime: str,
    target: str,
    follow: bool = False,
    limit_lines: int = 200,
    since: str = None,
    filter_regex: str = None,
    namespace: str = None,
    container: str = None,
    cluster: str = None,
    log_group: str = None,
    region: str = None
) -> str
```

Tails logs from running containers across three runtimes.

**Returns:** JSON with `logs` (list), `count`, `runtime`, `target`

---

## Runtime target syntax

### `docker`

```python
tail_container_logs(runtime="docker", target="my-container-name")
```

Runs `docker logs` against the named container. Use `follow=True` for streaming (capped at `limit_lines`).

### `k8s` (Kubernetes)

```python
tail_container_logs(
    runtime="k8s",
    target="my-pod-name",
    namespace="production",
    container="app"   # optional: specific container in multi-container pod
)
```

Runs `kubectl logs`. `namespace` defaults to `default`.

### `ecs` (AWS ECS via CloudWatch)

```python
tail_container_logs(
    runtime="ecs",
    target="my-task-id",
    cluster="my-cluster",
    log_group="/ecs/my-service",
    region="us-east-1"
)
```

Reads CloudWatch Logs for the ECS task. `cluster`, `log_group`, and `region` are required for ECS.

---

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `CLAUDE_HOOK_LOG_FILE` | No | `~/.agentihooks/logs/hooks.log` | Log file path for `read_session_logs` |
