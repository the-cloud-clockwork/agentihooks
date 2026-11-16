"""Tests for hooks.observability.container_logs module."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


class TestContainerLogTailerInit:
    """Test ContainerLogTailer initialization and validation."""

    def test_docker_init(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("docker", "my-container")
        assert tailer.runtime == "docker"
        assert tailer.target == "my-container"

    def test_k8s_init(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("k8s", "my-pod", namespace="prod")
        assert tailer.runtime == "k8s"
        assert tailer.target == "my-pod"
        assert tailer.kwargs["namespace"] == "prod"

    def test_ecs_init(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("ecs", "task-arn", cluster="my-cluster", log_group="/ecs/logs")
        assert tailer.runtime == "ecs"
        assert tailer.kwargs["cluster"] == "my-cluster"

    def test_invalid_runtime_raises(self):
        from hooks.observability.container_logs import ContainerLogTailer

        with pytest.raises(ValueError, match="Invalid runtime"):
            ContainerLogTailer("invalid", "target")

    def test_empty_target_raises(self):
        from hooks.observability.container_logs import ContainerLogTailer

        with pytest.raises(ValueError, match="Target container identifier is required"):
            ContainerLogTailer("docker", "")

    def test_ecs_missing_cluster_raises(self):
        from hooks.observability.container_logs import ContainerLogTailer

        with pytest.raises(ValueError, match="ECS runtime requires both"):
            ContainerLogTailer("ecs", "task-arn", log_group="/ecs/logs")

    def test_ecs_missing_log_group_raises(self):
        from hooks.observability.container_logs import ContainerLogTailer

        with pytest.raises(ValueError, match="ECS runtime requires both"):
            ContainerLogTailer("ecs", "task-arn", cluster="my-cluster")

    def test_ecs_missing_both_raises(self):
        from hooks.observability.container_logs import ContainerLogTailer

        with pytest.raises(ValueError, match="ECS runtime requires both"):
            ContainerLogTailer("ecs", "task-arn")


class TestBuildDockerCommand:
    """Test Docker command building."""

    def test_basic_docker_cmd(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("docker", "my-container")
        cmd = tailer._build_command(follow=False, limit_lines=100, since=None)
        assert cmd == ["docker", "logs", "--tail", "100", "my-container"]

    def test_docker_cmd_with_follow(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("docker", "my-container")
        cmd = tailer._build_command(follow=True, limit_lines=200, since=None)
        assert "--follow" in cmd

    def test_docker_cmd_with_since(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("docker", "my-container")
        cmd = tailer._build_command(follow=False, limit_lines=200, since="10m")
        assert "--since" in cmd
        idx = cmd.index("--since")
        assert cmd[idx + 1] == "10m"

    def test_docker_cmd_with_all_options(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("docker", "web-app")
        cmd = tailer._build_command(follow=True, limit_lines=50, since="1h")
        assert cmd[0] == "docker"
        assert "--follow" in cmd
        assert "--since" in cmd
        assert "--tail" in cmd
        assert "web-app" in cmd


class TestBuildK8sCommand:
    """Test Kubernetes command building."""

    def test_basic_k8s_cmd(self):
        """Default namespace."""
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("k8s", "my-pod")
        cmd = tailer._build_command(follow=False, limit_lines=100, since=None)
        assert cmd[0] == "kubectl"
        assert "my-pod" in cmd
        assert "-n" in cmd
        idx = cmd.index("-n")
        assert cmd[idx + 1] == "default"

    def test_k8s_custom_namespace(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("k8s", "my-pod", namespace="production")
        cmd = tailer._build_command(follow=False, limit_lines=100, since=None)
        idx = cmd.index("-n")
        assert cmd[idx + 1] == "production"

    def test_k8s_with_container(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("k8s", "my-pod", container="sidecar")
        cmd = tailer._build_command(follow=False, limit_lines=100, since=None)
        assert "--container" in cmd
        idx = cmd.index("--container")
        assert cmd[idx + 1] == "sidecar"

    def test_k8s_with_follow(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("k8s", "my-pod")
        cmd = tailer._build_command(follow=True, limit_lines=100, since=None)
        assert "-f" in cmd

    def test_k8s_with_since(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("k8s", "my-pod")
        cmd = tailer._build_command(follow=False, limit_lines=100, since="5m")
        assert "--since" in cmd


class TestBuildEcsCommand:
    """Test ECS command building."""

    def test_basic_ecs_cmd(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("ecs", "task-arn", cluster="my-cluster", log_group="/ecs/my-service")
        cmd = tailer._build_command(follow=False, limit_lines=100, since=None)
        assert cmd[0] == "aws"
        assert "logs" in cmd
        assert "tail" in cmd
        assert "/ecs/my-service" in cmd

    def test_ecs_with_follow(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("ecs", "task-arn", cluster="c", log_group="/ecs/svc")
        cmd = tailer._build_command(follow=True, limit_lines=100, since=None)
        assert "--follow" in cmd

    def test_ecs_with_region(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("ecs", "task-arn", cluster="c", log_group="/ecs/svc", region="eu-west-1")
        cmd = tailer._build_command(follow=False, limit_lines=100, since=None)
        assert "--region" in cmd
        idx = cmd.index("--region")
        assert cmd[idx + 1] == "eu-west-1"

    def test_ecs_with_since(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("ecs", "task-arn", cluster="c", log_group="/ecs/svc")
        cmd = tailer._build_command(follow=False, limit_lines=100, since="30m")
        assert "--since" in cmd


class TestStreamOutput:
    """Test _stream_output() subprocess handling."""

    def test_stream_output_basic(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("docker", "test")

        mock_process = MagicMock()
        mock_process.stdout = iter(["line1\n", "line2\n", "line3\n"])
        mock_process.wait.return_value = 0

        with patch("hooks.observability.container_logs.subprocess.Popen", return_value=mock_process):
            logs = tailer._stream_output(["docker", "logs", "test"], None)
            assert logs == ["line1", "line2", "line3"]

    def test_stream_output_with_filter(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("docker", "test")

        mock_process = MagicMock()
        mock_process.stdout = iter(["ERROR: something\n", "INFO: normal\n", "ERROR: another\n"])
        mock_process.wait.return_value = 0

        with patch("hooks.observability.container_logs.subprocess.Popen", return_value=mock_process):
            logs = tailer._stream_output(["docker", "logs", "test"], "ERROR")
            assert len(logs) == 2
            assert all("ERROR" in line for line in logs)

    def test_stream_output_invalid_regex(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("docker", "test")
        with pytest.raises(ValueError, match="Invalid regex pattern"):
            tailer._stream_output(["docker", "logs", "test"], "[invalid")

    def test_stream_output_nonzero_exit(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("docker", "test")

        mock_process = MagicMock()
        mock_process.stdout = iter([])
        mock_process.wait.return_value = 1

        with patch("hooks.observability.container_logs.subprocess.Popen", return_value=mock_process):
            with pytest.raises(subprocess.CalledProcessError):
                tailer._stream_output(["docker", "logs", "test"], None)

    def test_stream_output_command_not_found(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("docker", "test")

        with patch(
            "hooks.observability.container_logs.subprocess.Popen",
            side_effect=FileNotFoundError("docker not found"),
        ):
            with pytest.raises(FileNotFoundError, match="not found"):
                tailer._stream_output(["docker", "logs", "test"], None)


class TestTail:
    """Test the tail() method end-to-end with mocked subprocess."""

    def test_tail_calls_build_and_stream(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("docker", "web")

        mock_process = MagicMock()
        mock_process.stdout = iter(["log line 1\n", "log line 2\n"])
        mock_process.wait.return_value = 0

        with patch("hooks.observability.container_logs.subprocess.Popen", return_value=mock_process):
            logs = tailer.tail(follow=False, limit_lines=100)
            assert logs == ["log line 1", "log line 2"]

    def test_tail_with_filter(self):
        from hooks.observability.container_logs import ContainerLogTailer

        tailer = ContainerLogTailer("docker", "web")

        mock_process = MagicMock()
        mock_process.stdout = iter(["INFO ok\n", "WARN bad\n", "INFO good\n"])
        mock_process.wait.return_value = 0

        with patch("hooks.observability.container_logs.subprocess.Popen", return_value=mock_process):
            logs = tailer.tail(filter_regex="WARN")
            assert len(logs) == 1
            assert "WARN" in logs[0]
