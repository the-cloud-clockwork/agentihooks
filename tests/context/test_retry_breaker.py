"""Tests for hooks.context.retry_breaker — Retry Circuit Breaker."""

from unittest.mock import patch

import pytest

# Import module once so we can clear its state reliably
import hooks.context.retry_breaker as _breaker_mod


@pytest.fixture(autouse=True)
def no_redis():
    """Disable Redis for all tests — use memory fallback."""
    with patch.object(_breaker_mod, "get_redis", return_value=None):
        yield


@pytest.fixture(autouse=True)
def clear_memory():
    """Reset in-memory state between tests."""
    _breaker_mod._memory_state.clear()
    yield
    _breaker_mod._memory_state.clear()


class TestComputeOperationKey:
    def test_bash_npm(self):
        from hooks.context.retry_breaker import _compute_operation_key

        assert _compute_operation_key("Bash", {"command": "npm install react"}) == "bash:npm"

    def test_bash_pip(self):
        from hooks.context.retry_breaker import _compute_operation_key

        assert _compute_operation_key("Bash", {"command": "pip install flask"}) == "bash:pip"

    def test_bash_with_sudo(self):
        from hooks.context.retry_breaker import _compute_operation_key

        assert _compute_operation_key("Bash", {"command": "sudo npm install"}) == "bash:npm"

    def test_bash_with_env_var(self):
        from hooks.context.retry_breaker import _compute_operation_key

        assert _compute_operation_key("Bash", {"command": "NODE_ENV=prod npm start"}) == "bash:npm"

    def test_bash_with_path(self):
        from hooks.context.retry_breaker import _compute_operation_key

        assert _compute_operation_key("Bash", {"command": "/usr/bin/python3 script.py"}) == "bash:python3"

    def test_bash_empty(self):
        from hooks.context.retry_breaker import _compute_operation_key

        assert _compute_operation_key("Bash", {"command": ""}) == "bash:unknown"

    def test_bash_no_input(self):
        from hooks.context.retry_breaker import _compute_operation_key

        assert _compute_operation_key("Bash", {}) == "bash:unknown"

    def test_other_tools(self):
        from hooks.context.retry_breaker import _compute_operation_key

        assert _compute_operation_key("Write", {}) == "write"
        assert _compute_operation_key("Edit", {}) == "edit"
        assert _compute_operation_key("Read", {}) == "read"
        assert _compute_operation_key("mcp__github-list_issues", {}) == "mcp__github-list_issues"


class TestSubcommandGrouping:
    """DevOps tools use subcommand-level grouping."""

    def test_terraform_subcommands(self):
        from hooks.context.retry_breaker import _compute_operation_key

        assert _compute_operation_key("Bash", {"command": "terraform plan"}) == "bash:terraform:plan"
        assert _compute_operation_key("Bash", {"command": "terraform apply -auto-approve"}) == "bash:terraform:apply"
        assert _compute_operation_key("Bash", {"command": "terraform destroy"}) == "bash:terraform:destroy"
        assert _compute_operation_key("Bash", {"command": "terraform init"}) == "bash:terraform:init"

    def test_kubectl_subcommands(self):
        from hooks.context.retry_breaker import _compute_operation_key

        assert _compute_operation_key("Bash", {"command": "kubectl apply -f deploy.yaml"}) == "bash:kubectl:apply"
        assert _compute_operation_key("Bash", {"command": "kubectl get pods -n prod"}) == "bash:kubectl:get"
        assert _compute_operation_key("Bash", {"command": "kubectl delete pod my-pod"}) == "bash:kubectl:delete"

    def test_kubectl_alias(self):
        from hooks.context.retry_breaker import _compute_operation_key

        assert _compute_operation_key("Bash", {"command": "k apply -f deploy.yaml"}) == "bash:k:apply"
        assert _compute_operation_key("Bash", {"command": "k get pods"}) == "bash:k:get"

    def test_argocd_two_level(self):
        from hooks.context.retry_breaker import _compute_operation_key

        assert _compute_operation_key("Bash", {"command": "argocd app sync myapp"}) == "bash:argocd:app:sync"
        assert _compute_operation_key("Bash", {"command": "argocd app get myapp"}) == "bash:argocd:app:get"
        assert _compute_operation_key("Bash", {"command": "argocd cluster list"}) == "bash:argocd:cluster:list"

    def test_aws_two_level(self):
        from hooks.context.retry_breaker import _compute_operation_key

        assert _compute_operation_key("Bash", {"command": "aws s3 cp file.txt s3://bucket/"}) == "bash:aws:s3:cp"
        assert _compute_operation_key("Bash", {"command": "aws ec2 describe-instances"}) == "bash:aws:ec2:describe-instances"
        assert _compute_operation_key("Bash", {"command": "aws sts get-caller-identity"}) == "bash:aws:sts:get-caller-identity"

    def test_helm(self):
        from hooks.context.retry_breaker import _compute_operation_key

        assert _compute_operation_key("Bash", {"command": "helm install my-release chart/"}) == "bash:helm:install"
        assert _compute_operation_key("Bash", {"command": "helm upgrade my-release chart/"}) == "bash:helm:upgrade"

    def test_docker(self):
        from hooks.context.retry_breaker import _compute_operation_key

        assert _compute_operation_key("Bash", {"command": "docker build -t myimg ."}) == "bash:docker:build"
        assert _compute_operation_key("Bash", {"command": "docker push myimg"}) == "bash:docker:push"

    def test_gcloud(self):
        from hooks.context.retry_breaker import _compute_operation_key

        assert _compute_operation_key("Bash", {"command": "gcloud compute instances list"}) == "bash:gcloud:compute"

    def test_az(self):
        from hooks.context.retry_breaker import _compute_operation_key

        assert _compute_operation_key("Bash", {"command": "az vm list"}) == "bash:az:vm"

    def test_non_subcommand_tools_unchanged(self):
        from hooks.context.retry_breaker import _compute_operation_key

        assert _compute_operation_key("Bash", {"command": "npm install"}) == "bash:npm"
        assert _compute_operation_key("Bash", {"command": "pip install flask"}) == "bash:pip"
        assert _compute_operation_key("Bash", {"command": "cargo build"}) == "bash:cargo"

    def test_subcommand_with_flags_only(self):
        from hooks.context.retry_breaker import _compute_operation_key

        # No subcommand token (only flags) — falls back to base only
        assert _compute_operation_key("Bash", {"command": "terraform --version"}) == "bash:terraform"


class TestComputeErrorKey:
    def test_strips_numbers(self):
        from hooks.context.retry_breaker import _compute_error_key

        key1 = _compute_error_key("error at line 42")
        key2 = _compute_error_key("error at line 99")
        assert key1 == key2

    def test_strips_paths(self):
        from hooks.context.retry_breaker import _compute_error_key

        key1 = _compute_error_key("ENOENT: no such file /home/user/project/foo.js")
        key2 = _compute_error_key("ENOENT: no such file /tmp/bar.js")
        assert key1 == key2

    def test_strips_hex(self):
        from hooks.context.retry_breaker import _compute_error_key

        key1 = _compute_error_key("segfault at 0xDEADBEEF")
        key2 = _compute_error_key("segfault at 0x12345678")
        assert key1 == key2

    def test_case_insensitive(self):
        from hooks.context.retry_breaker import _compute_error_key

        key1 = _compute_error_key("ModuleNotFoundError: No module named 'flask'")
        key2 = _compute_error_key("modulenotfounderror: no module named 'flask'")
        assert key1 == key2

    def test_truncates(self):
        from hooks.context.retry_breaker import _compute_error_key

        long_error = "x" * 200
        assert len(_compute_error_key(long_error)) <= 80


class TestFailureTracking:
    def _make_error_payload(self, tool_name="Bash", command="npm install", error_text="ENOENT not found", session_id="track-session"):
        return {
            "tool_name": tool_name,
            "tool_input": {"command": command} if tool_name == "Bash" else {},
            "tool_response": {"is_error": True, "content": error_text},
            "session_id": session_id,
        }

    def _make_success_payload(self, tool_name="Bash", command="npm install", session_id="track-session"):
        return {
            "tool_name": tool_name,
            "tool_input": {"command": command} if tool_name == "Bash" else {},
            "tool_response": {"exitCode": 0, "stdout": "OK"},
            "session_id": session_id,
        }

    def test_increments_on_same_error(self):
        from hooks.context.retry_breaker import _compute_operation_key, _get_state, on_post_tool_result

        payload = self._make_error_payload(session_id="incr-session")
        on_post_tool_result(payload)
        on_post_tool_result(payload)

        op_key = _compute_operation_key("Bash", {"command": "npm install"})
        state = _get_state("incr-session", op_key)
        assert state["count"] == 2

    def test_resets_on_different_error(self):
        from hooks.context.retry_breaker import _compute_operation_key, _get_state, on_post_tool_result

        payload1 = self._make_error_payload(error_text="ENOENT not found", session_id="diff-err")
        payload2 = self._make_error_payload(error_text="permission denied", session_id="diff-err")

        on_post_tool_result(payload1)
        on_post_tool_result(payload1)
        on_post_tool_result(payload2)

        op_key = _compute_operation_key("Bash", {"command": "npm install"})
        state = _get_state("diff-err", op_key)
        assert state["count"] == 1  # Reset because different error

    def test_resets_on_success(self):
        from hooks.context.retry_breaker import _compute_operation_key, _get_state, on_post_tool_result

        error_payload = self._make_error_payload(session_id="succ-reset")
        success_payload = self._make_success_payload(session_id="succ-reset")

        on_post_tool_result(error_payload)
        on_post_tool_result(error_payload)
        on_post_tool_result(success_payload)

        op_key = _compute_operation_key("Bash", {"command": "npm install"})
        state = _get_state("succ-reset", op_key)
        assert state["count"] == 0

    def test_no_session_id_is_noop(self):
        from hooks.context.retry_breaker import on_post_tool_result

        payload = self._make_error_payload(session_id="")
        on_post_tool_result(payload)  # Should not raise

    def test_no_tool_result_is_noop(self):
        from hooks.context.retry_breaker import on_post_tool_result

        payload = self._make_error_payload(session_id="noop-session")
        del payload["tool_response"]
        on_post_tool_result(payload)  # Should not raise


class TestBreakerTrip:
    def _make_error_payload(self, session_id="trip-session", error_text="ENOENT not found"):
        return {
            "tool_name": "Bash",
            "tool_input": {"command": "npm install"},
            "tool_response": {"is_error": True, "content": error_text},
            "session_id": session_id,
        }

    @patch("hooks.context.retry_breaker._inject_breaker_message")
    def test_trips_at_threshold(self, mock_inject):
        from hooks.context.retry_breaker import on_post_tool_result

        payload = self._make_error_payload(session_id="trip-thresh")
        for _ in range(4):
            on_post_tool_result(payload)
        assert mock_inject.call_count == 0

        on_post_tool_result(payload)  # 5 — trips
        assert mock_inject.call_count == 1

    @patch("hooks.context.retry_breaker._inject_breaker_message")
    def test_keeps_firing_after_threshold(self, mock_inject):
        from hooks.context.retry_breaker import on_post_tool_result

        payload = self._make_error_payload(session_id="trip-keeps")
        for _ in range(7):
            on_post_tool_result(payload)

        # Fires every time count >= 5 (at counts 5, 6, 7)
        assert mock_inject.call_count == 3

    @patch("hooks.common.inject_banner")
    def test_inject_message_contains_error_context(self, mock_banner):
        from hooks.context.retry_breaker import _inject_breaker_message

        _inject_breaker_message("Bash", {"command": "npm install"}, "ENOENT not found", 3)
        mock_banner.assert_called_once()
        title, body = mock_banner.call_args[0]
        assert "CIRCUIT BREAKER" in title
        assert "3" in title
        assert "error-researcher" in body
        assert "ENOENT not found" in body


class TestHardBlock:
    def test_blocks_at_hard_max(self):
        from hooks.context.retry_breaker import _set_state, check_hard_block
        from hooks.hook_manager import BlockAction

        _set_state("block-session", "bash:npm", {
            "count": 10,
            "last_error_key": "enoent",
            "last_error_text": "ENOENT not found",
            "last_input": "npm install",
        })

        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "npm install"},
            "session_id": "block-session",
        }

        with pytest.raises(BlockAction) as exc_info:
            check_hard_block(payload)

        assert "hard stop" in str(exc_info.value).lower()
        assert "bash:npm" in str(exc_info.value)

    def test_does_not_block_below_hard_max(self):
        from hooks.context.retry_breaker import _set_state, check_hard_block

        _set_state("noblock-session", "bash:npm", {
            "count": 9,
            "last_error_key": "enoent",
            "last_error_text": "ENOENT not found",
            "last_input": "npm install",
        })

        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "npm install"},
            "session_id": "noblock-session",
        }

        check_hard_block(payload)  # Should not raise


class TestClearState:
    def test_clears_memory(self):
        from hooks.context.retry_breaker import _get_state, _set_state, clear_session_state

        _set_state("clear-session", "bash:npm", {"count": 3, "last_error_key": "x", "last_error_text": "y", "last_input": "z"})
        assert _get_state("clear-session", "bash:npm")["count"] == 3

        clear_session_state("clear-session")
        assert _get_state("clear-session", "bash:npm")["count"] == 0
