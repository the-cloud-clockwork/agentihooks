"""Failure and fallback paths of the hook integrations: hook mode exits 0, clients log and return."""

import importlib
import io
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest

from hooks.integrations import dynamodb, mailer, postgres, session_state, storage

lambda_invoke = importlib.import_module("hooks.integrations.lambda_invoke")

pytestmark = pytest.mark.unit

POSTGRES_ENV = ("POSTGRES_HOST", "POSTGRES_NAME", "POSTGRES_USERNAME", "POSTGRES_PASSWORD")


class _Failing:
    """Object whose every attribute call raises."""

    def __getattr__(self, name: str) -> Any:
        """Return a callable that raises RuntimeError."""

        def fail(*args: Any, **kwargs: Any) -> Any:
            """Raise RuntimeError."""
            raise RuntimeError(name)

        return fail


class _BrokenConnection:
    """Database connection whose cursor, rollback and close all raise."""

    closed = False

    def cursor(self) -> Any:
        """Raise RuntimeError."""
        raise RuntimeError("cursor")

    def rollback(self) -> None:
        """Raise RuntimeError."""
        raise RuntimeError("rollback")

    def close(self) -> None:
        """Raise RuntimeError."""
        raise RuntimeError("close")


class _LambdaClient:
    """Lambda client returning a sync response whose payload is not JSON."""

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        """Return status 200 with an unreadable payload."""
        return {"StatusCode": 200, "Payload": io.BytesIO(b"not json")}


class _S3Client:
    """S3 client recording uploaded keys."""

    def __init__(self) -> None:
        """Start with no keys."""
        self.keys: list[str] = []

    def put_object(self, **kwargs: Any) -> None:
        """Record the uploaded key."""
        self.keys.append(kwargs["Key"])


def _run_hook(module: ModuleType, monkeypatch: pytest.MonkeyPatch, argv: list[str], stdin: str) -> int:
    """Run a module's CLI in hook mode and return its exit code."""
    monkeypatch.setattr(sys, "argv", [module.__name__, "hook", *argv])
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    with pytest.raises(SystemExit) as exited:
        module.main()
    return int(exited.value.code or 0)


@pytest.mark.parametrize(
    ("module", "env"),
    [
        (dynamodb, ("DYNAMODB_TABLE_NAME",)),
        (lambda_invoke, ("LAMBDA_FUNCTION_NAME",)),
        (postgres, POSTGRES_ENV),
        (storage, ("STORAGE_URL",)),
    ],
)
def test_hook_without_configuration_warns_and_exits_zero(
    module: ModuleType, env: tuple[str, ...], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unconfigured hook prints its error to stdout and exits 0."""
    for name in env:
        monkeypatch.delenv(name, raising=False)
    assert _run_hook(module, monkeypatch, [], "{}") == 0
    assert "ERROR" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("module", "env", "argv"),
    [
        (dynamodb, {"DYNAMODB_TABLE_NAME": "events"}, []),
        (lambda_invoke, {"LAMBDA_FUNCTION_NAME": "handler"}, []),
        (postgres, dict.fromkeys(POSTGRES_ENV, "configured"), ["--table", "events"]),
        (storage, {"STORAGE_URL": "s3://bucket/base"}, ["--path", "/nonexistent"]),
    ],
)
def test_hook_failure_exits_zero(
    module: ModuleType, env: dict[str, str], argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured hook that fails on its input still exits 0."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert _run_hook(module, monkeypatch, argv, "not json") == 0


def test_lambda_sync_invoke_tolerates_unreadable_payload() -> None:
    """A sync invoke with status 200 succeeds when its payload is not JSON."""
    client = lambda_invoke.LambdaClient(function_name="handler", invocation_type="RequestResponse")
    client._lambda_client = _LambdaClient()
    result = client.invoke({"event": "done"})
    assert result.success is True
    assert result.response_payload is None


def test_postgres_failures_never_raise() -> None:
    """Insert, execute and cache clearing swallow connection, rollback and close errors."""
    client = postgres.PostgresClient(host="h", database="d", username="u", password="p", skip_evaluation=False)
    client._connection = _BrokenConnection()
    assert client.insert("events", {"session_id": "s"}).success is False
    assert client.execute("SELECT 1").success is False
    postgres.PostgresClient._instance = client
    postgres.PostgresClient.clear_cache()
    assert postgres.PostgresClient._instance is None


def test_session_state_falls_back_to_file_when_redis_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis errors fall back to the session map file, and deleting an absent session succeeds."""
    monkeypatch.setattr(session_state, "SESSION_MAP_FILE", tmp_path / "conversation_map.json")
    with patch("hooks._redis.get_redis", return_value=_Failing()):
        assert session_state.enrich_session("s1", {"k": "v"}) is True
        assert session_state.get_session("s1") == {"k": "v"}
        assert session_state.delete_session("s1") is True
    with patch("hooks._redis.get_redis", return_value=None):
        assert session_state.delete_session("absent") is True


def test_storage_directory_upload_keys_under_base_prefix(tmp_path: Path) -> None:
    """A directory upload keys each file under the base prefix and the artifacts folder."""
    root = tmp_path / "upload"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "s1-out.txt").write_text("x")
    client = storage.S3StorageClient(storage_url="s3://bucket/base/", skip_evaluation=False)
    s3 = _S3Client()
    client._s3_client = s3
    result = client.upload_path("s1", str(root))
    assert result.files_uploaded == 1
    assert s3.keys == ["base/artifacts/nested/s1-out.txt"]


def test_email_client_defaults_to_starttls_port_with_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """An authenticated client with no explicit port uses 587."""
    monkeypatch.setenv("SMTP_USER", "user")
    monkeypatch.setenv("SMTP_PASS", "token")
    monkeypatch.delenv("SMTP_PORT", raising=False)
    assert mailer.EmailClient(smtp_server="smtp.example.com").smtp_port == 587


def test_email_config_falls_back_to_default_recipients() -> None:
    """A category without recipients falls back to the config recipients."""
    config = mailer.EmailConfig(
        version="2",
        categories=mailer.EmailRecipientCategories([], [], []),
        recipients=["fallback@example.com"],
        defaults={},
    )
    assert config.get_recipients_for_category("error_recipients") == ["fallback@example.com"]


def test_template_variables_are_replaced_and_leftovers_kept() -> None:
    """Known placeholders are replaced and unknown ones stay in the output."""
    html = mailer.inject_template_variables("{{name}} {{missing}}", {"name": "Ada"})
    assert html == "Ada {{missing}}"


def test_mailer_cli_without_command_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mailer CLI prints usage and exits 1 without a command."""
    monkeypatch.setattr(sys, "argv", ["mailer.py"])
    with pytest.raises(SystemExit) as exited:
        mailer.main()
    assert exited.value.code == 1
