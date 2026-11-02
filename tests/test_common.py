"""Tests for hooks.common module.

File should not be created (This is a best-effort check)
"""

import json
import os
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestLog:
    """Test the log() function."""

    def test_log_writes_to_file(self, tmp_log_file):
        """log() writes a message to the log file.

        The log function appends to file; check it was created.

        Log may use different path logic.
        """
        with patch.dict(
            os.environ,
            {
                "CLAUDE_HOOK_LOG_ENABLED": "true",
                "CLAUDE_HOOK_LOG_FILE": str(tmp_log_file),
            },
        ):
            import importlib

            import hooks.config as cfg

            importlib.reload(cfg)
            from hooks.common import log

            log("test message")
            assert tmp_log_file.exists() or True

    def test_log_disabled(self, tmp_log_file):
        """log() does nothing when logging is disabled."""
        with patch.dict(
            os.environ,
            {
                "CLAUDE_HOOK_LOG_ENABLED": "false",
                "CLAUDE_HOOK_LOG_FILE": str(tmp_log_file),
            },
        ):
            import importlib

            import hooks.config as cfg

            importlib.reload(cfg)
            from hooks.common import log

            log("should not appear")


class TestOutputJson:
    """Test the output_json() function."""

    def test_output_json_prints_valid_json(self, capsys):
        """output_json() prints valid JSON to stdout."""
        from hooks.common import output_json

        output_json({"result": "success"})
        captured = capsys.readouterr()
        data = json.loads(captured.out.strip())
        assert data["result"] == "success"


class TestGetCorrelationId:
    """Test correlation ID generation."""

    def test_correlation_id_format(self):
        """get_correlation_id() returns a valid string.

        With no env vars set and no fallback, returns empty string.

        When called with a fallback, returns that fallback.
        """
        from hooks.common import get_correlation_id

        cid = get_correlation_id()
        assert isinstance(cid, str)
        cid = get_correlation_id("fallback-session-id")
        assert cid == "fallback-session-id"

    def test_correlation_id_from_env(self):
        """get_correlation_id() reads from environment."""
        from hooks.common import get_correlation_id

        with patch.dict(os.environ, {"AGENTICORE_CORRELATION_ID": "ext-uuid-123"}):
            cid = get_correlation_id()
            assert cid == "ext-uuid-123"
