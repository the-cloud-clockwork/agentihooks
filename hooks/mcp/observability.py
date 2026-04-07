"""Observability MCP tools — session log reading and container log tailing."""

import json
from pathlib import Path
from typing import Optional

from hooks.common import log


def register(mcp):
    @mcp.tool()
    def read_session_logs(
        session_id: str = "",
        level: str = "",
        event: str = "",
        tail: int = 100,
    ) -> str:
        """Read hooks log entries, optionally filtered by session, level, or event.

        The hooks log (~/.agentihooks/logs/hooks.log) captures every hook event:
        MCP failures, secrets warnings, context refresh, retry breaker trips,
        file read cache blocks, token warnings, and more.

        Args:
            session_id: Filter to this session ID (default: all sessions).
                        Partial match supported — e.g. "e361d38d" matches.
            level: Filter by message keyword — e.g. "error", "warning", "failed",
                   "blocked", "breaker". Case-insensitive substring match.
            event: Filter by event type in message — e.g. "Pre tool use",
                   "Post tool use", "context_refresh", "retry_breaker".
            tail: Number of matching lines to return, most recent first (default: 100).

        Returns:
            JSON with matching log entries and count.
        """
        try:
            from hooks.config import LOG_FILE

            log_path = Path(LOG_FILE)
            if not log_path.exists():
                return json.dumps({"success": False, "error": f"Log file not found: {LOG_FILE}"})

            matches = []
            for line in log_path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if not isinstance(entry, dict):
                        continue
                except (json.JSONDecodeError, ValueError):
                    continue

                # Filter by session_id
                payload = entry.get("payload") or {}
                if isinstance(payload, str):
                    payload = {}
                if session_id:
                    entry_sid = payload.get("session_id", "")
                    if session_id not in entry_sid:
                        continue

                msg = entry.get("message", "")

                # Filter by level keyword
                if level and level.lower() not in msg.lower():
                    payload_str = json.dumps(payload).lower()
                    if level.lower() not in payload_str:
                        continue

                # Filter by event type
                if event and event.lower() not in msg.lower():
                    continue

                matches.append(entry)

            # Return most recent N
            results = matches[-tail:]

            return json.dumps({
                "success": True,
                "count": len(results),
                "total_matches": len(matches),
                "entries": results,
            })

        except Exception as e:
            log("MCP read_session_logs failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def tail_container_logs(
        runtime: str,
        target: str,
        follow: bool = False,
        limit_lines: int = 200,
        since: Optional[str] = None,
        filter_regex: Optional[str] = None,
        namespace: Optional[str] = None,
        container: Optional[str] = None,
        cluster: Optional[str] = None,
        log_group: Optional[str] = None,
        region: Optional[str] = None,
    ) -> str:
        """Tail logs from a container across Docker, Kubernetes, or AWS ECS.

        Args:
            runtime: REQUIRED - 'docker', 'k8s', or 'ecs'
            target: REQUIRED - Container ID/name, pod name, or task ARN
            follow: Stream logs continuously (default: False for last N lines)
            limit_lines: Number of recent lines to show (default: 200)
            since: Time duration (e.g., '10m', '1h')
            filter_regex: Client-side regex filter for log lines
            namespace: (K8s only) Kubernetes namespace (default: 'default')
            container: (K8s only) Container name in pod (if multi-container)
            cluster: (ECS only) REQUIRED - ECS cluster name
            log_group: (ECS only) REQUIRED - CloudWatch log group
            region: (ECS only) AWS region (optional)

        Returns:
            JSON with logs list and count
        """
        try:
            from hooks.observability.container_logs import ContainerLogTailer

            kwargs = {}
            if namespace:
                kwargs["namespace"] = namespace
            if container:
                kwargs["container"] = container
            if cluster:
                kwargs["cluster"] = cluster
            if log_group:
                kwargs["log_group"] = log_group
            if region:
                kwargs["region"] = region

            tailer = ContainerLogTailer(runtime, target, **kwargs)
            logs = tailer.tail(
                follow=follow,
                limit_lines=limit_lines,
                since=since,
                filter_regex=filter_regex,
            )

            return json.dumps({
                "success": True,
                "logs": logs,
                "count": len(logs),
                "runtime": runtime,
                "target": target,
            })

        except ValueError as e:
            log("MCP tail_container_logs validation failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

        except FileNotFoundError as e:
            log("MCP tail_container_logs command not found", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

        except Exception as e:
            log("MCP tail_container_logs failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})
