"""
Unified container log tailer for Docker, Kubernetes, and AWS ECS.

This module provides a ContainerLogTailer class that can tail logs from containers
across different runtime environments, exposing a consistent interface for agents
to troubleshoot their own logs.
"""

import re
import subprocess
from typing import List, Optional

from ..common import log


class ContainerLogTailer:
    """
    Tail logs from containers across Docker, Kubernetes, and AWS ECS runtimes.

    Supports streaming logs with optional filtering and time-based queries.
    """

    SUPPORTED_RUNTIMES = {"docker", "k8s", "ecs"}

    def __init__(self, runtime: str, target: str, **kwargs):
        """
        Initialize the container log tailer.

        Args:
            runtime: Runtime type - 'docker', 'k8s', or 'ecs'
            target: Container identifier (container ID/name, pod name, or task ARN)
            **kwargs: Runtime-specific parameters
                - Docker: (none currently)
                - K8s: namespace (default: 'default'), container (optional)
                - ECS: cluster (REQUIRED), log_group (REQUIRED), region (optional)

        Raises:
            ValueError: If runtime is invalid or required parameters are missing
        """
        # Validate runtime
        if runtime not in self.SUPPORTED_RUNTIMES:
            raise ValueError(f"Invalid runtime '{runtime}'. Must be one of: {', '.join(self.SUPPORTED_RUNTIMES)}")

        # Validate target
        if not target:
            raise ValueError("Target container identifier is required")

        # Sanitize all string parameters — prevent flag injection
        _SAFE_ID = re.compile(r"^[a-zA-Z0-9._:/@-]+$")
        for param_name, param_val in [("target", target)] + [(k, v) for k, v in kwargs.items() if isinstance(v, str)]:
            if param_val and not _SAFE_ID.match(param_val):
                raise ValueError(
                    f"Invalid characters in '{param_name}': only alphanumeric, dots, dashes, underscores, colons, slashes, and @ are allowed"
                )

        self.runtime = runtime
        self.target = target
        self.kwargs = kwargs

        # ECS-specific validation
        if runtime == "ecs":
            cluster = kwargs.get("cluster")
            log_group = kwargs.get("log_group")

            if not cluster or not log_group:
                raise ValueError("ECS runtime requires both 'cluster' and 'log_group' parameters")

        log(
            "ContainerLogTailer initialized",
            {"runtime": runtime, "target": target, "kwargs": kwargs},
        )

    def tail(
        self,
        follow: bool = False,
        limit_lines: int = 200,
        since: Optional[str] = None,
        filter_regex: Optional[str] = None,
    ) -> List[str]:
        """
        Tail logs from the target container.

        Args:
            follow: Stream logs continuously (default: False)
            limit_lines: Number of recent lines to show (default: 200)
            since: Time duration (e.g., '10m', '1h')
            filter_regex: Client-side regex filter for log lines

        Returns:
            List of log lines (filtered if regex provided)

        Raises:
            subprocess.CalledProcessError: If the underlying command fails
            FileNotFoundError: If the runtime command (docker/kubectl/aws) is not found
        """
        # Build runtime-specific command
        cmd = self._build_command(follow, limit_lines, since)

        log(
            "Executing container log tail",
            {
                "command": cmd,
                "follow": follow,
                "limit_lines": limit_lines,
                "filter_regex": filter_regex,
            },
        )

        # Stream output
        logs = self._stream_output(cmd, filter_regex)

        log("Container log tail completed", {"lines_retrieved": len(logs)})

        return logs

    def _build_command(self, follow: bool, limit_lines: int, since: Optional[str]) -> List[str]:
        """
        Build the platform-specific command for tailing logs.

        Args:
            follow: Whether to stream logs continuously
            limit_lines: Number of recent lines to show
            since: Time duration filter

        Returns:
            Command as list of strings
        """
        if self.runtime == "docker":
            return self._build_docker_cmd(follow, limit_lines, since)
        elif self.runtime == "k8s":
            return self._build_k8s_cmd(follow, limit_lines, since)
        elif self.runtime == "ecs":
            return self._build_ecs_cmd(follow, limit_lines, since)
        else:
            raise ValueError(f"Unsupported runtime: {self.runtime}")

    def _build_docker_cmd(self, follow: bool, limit_lines: int, since: Optional[str]) -> List[str]:
        """Build Docker logs command."""
        cmd = ["docker", "logs", "--tail", str(limit_lines)]

        if follow:
            cmd.append("--follow")

        if since:
            cmd.extend(["--since", since])

        # Add target container
        cmd.append(self.target)

        return cmd

    def _build_k8s_cmd(self, follow: bool, limit_lines: int, since: Optional[str]) -> List[str]:
        """Build Kubernetes logs command."""
        namespace = self.kwargs.get("namespace", "default")
        container = self.kwargs.get("container")

        cmd = ["kubectl", "logs", "-n", namespace, "--tail", str(limit_lines)]

        if follow:
            cmd.append("-f")

        if since:
            cmd.extend(["--since", since])

        if container:
            cmd.extend(["--container", container])

        # Add target pod
        cmd.append(self.target)

        return cmd

    def _build_ecs_cmd(self, follow: bool, limit_lines: int, since: Optional[str]) -> List[str]:
        """
        Build AWS ECS (CloudWatch) logs command.

        Note: Requires cluster and log_group to be provided in kwargs.
        Validation is performed in __init__.
        """
        log_group = self.kwargs["log_group"]
        region = self.kwargs.get("region")

        cmd = ["aws", "logs", "tail", log_group]

        if follow:
            cmd.append("--follow")

        if since:
            cmd.extend(["--since", since])

        if region:
            cmd.extend(["--region", region])

        return cmd

    def _stream_output(self, cmd: List[str], filter_regex: Optional[str]) -> List[str]:
        """
        Stream subprocess output line-by-line with optional regex filtering.

        Note: Docker logs output to stderr by default, so we capture both streams.

        Args:
            cmd: Command to execute
            filter_regex: Optional regex pattern to filter log lines

        Returns:
            List of log lines (filtered if regex provided)

        Raises:
            subprocess.CalledProcessError: If the command fails
            FileNotFoundError: If the command executable is not found
        """
        pattern = None
        if filter_regex:
            if len(filter_regex) > 200:
                raise ValueError("Regex too long (max 200 chars) — simplify the filter")
            try:
                pattern = re.compile(filter_regex)
            except re.error as e:
                log("Invalid regex pattern", {"error": str(e), "pattern": filter_regex})
                raise ValueError(f"Invalid regex pattern: {e}")

        logs = []

        try:
            # Combine stdout and stderr for docker logs (which outputs to stderr)
            # Use STDOUT for stderr to combine streams
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # Redirect stderr to stdout
                text=True,
                bufsize=1,
            )

            # Stream combined output line-by-line
            for line in process.stdout:
                line = line.rstrip("\n")

                # Apply filter if provided
                if pattern is None or pattern.search(line):
                    logs.append(line)

            # Wait for process to complete
            return_code = process.wait()

            if return_code != 0:
                log(
                    "Container log tail command failed",
                    {"exit_code": return_code, "command": cmd},
                )
                raise subprocess.CalledProcessError(return_code, cmd)

            return logs

        except KeyboardInterrupt:
            # Handle Ctrl+C gracefully
            log("Container log tail interrupted by user", {})
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            return logs

        except FileNotFoundError as e:
            # Command not found (docker/kubectl/aws missing)
            log(
                "Container log tail command not found",
                {"command": cmd[0], "error": str(e)},
            )
            raise FileNotFoundError(f"Command '{cmd[0]}' not found. Is it installed and in PATH?")

        except Exception as e:
            log("Container log stream error", {"error": str(e), "command": cmd})
            raise


# Export for convenience
__all__ = ["ContainerLogTailer"]
