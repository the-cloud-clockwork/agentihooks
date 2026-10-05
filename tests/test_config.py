"""Tests for hooks.config module."""

import os
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestConfig:
    """Test configuration loading."""

    def test_log_enabled_default(self):
        """LOG_ENABLED reads from CLAUDE_HOOK_LOG_ENABLED."""
        with patch.dict(os.environ, {"CLAUDE_HOOK_LOG_ENABLED": "true"}):
            # Re-import to pick up env
            import importlib

            import hooks.config as cfg

            importlib.reload(cfg)
            assert cfg.LOG_ENABLED is True

    def test_log_enabled_false(self):
        """LOG_ENABLED is False when env var is not 'true'."""
        with patch.dict(os.environ, {"CLAUDE_HOOK_LOG_ENABLED": "false"}):
            import importlib

            import hooks.config as cfg

            importlib.reload(cfg)
            assert cfg.LOG_ENABLED is False

    def test_log_file_default(self):
        """LOG_FILE has a default value."""
        import hooks.config as cfg

        assert cfg.LOG_FILE is not None

    def test_memory_auto_save_default(self):
        """MEMORY_AUTO_SAVE reads from environment."""
        with patch.dict(os.environ, {"MEMORY_AUTO_SAVE": "true"}):
            import importlib

            import hooks.config as cfg

            importlib.reload(cfg)
            assert cfg.MEMORY_AUTO_SAVE is True


class TestSecretsMode:
    """Tests for SECRETS_MODE configuration."""

    @pytest.fixture(autouse=True)
    def _reload_config_after(self):
        yield
        import importlib

        import hooks.config as cfg

        importlib.reload(cfg)

    def _reload_with_mode(self, tmp_path, mode_value=None):
        """Reload hooks.config with AGENTIHOOKS_HOME pointing to an empty tmp dir
        so that no real .env files are loaded, then optionally set SECRETS_MODE."""
        import importlib

        import hooks.config as cfg

        env_overrides = {"AGENTIHOOKS_HOME": str(tmp_path)}
        if mode_value is not None:
            env_overrides["AGENTIHOOKS_SECRETS_MODE"] = mode_value
        with patch.dict(os.environ, env_overrides, clear=False):
            os.environ.pop("AGENTIHOOKS_SECRETS_MODE", None) if mode_value is None else None
            importlib.reload(cfg)
            return cfg.SECRETS_MODE

    def test_secrets_mode_default(self, tmp_path):
        """SECRETS_MODE defaults to 'standard' when env var is not set."""
        assert self._reload_with_mode(tmp_path, None) == "standard"

    def test_secrets_mode_reads_env(self, tmp_path):
        """SECRETS_MODE reads AGENTIHOOKS_SECRETS_MODE from env."""
        assert self._reload_with_mode(tmp_path, "strict") == "strict"

    def test_secrets_mode_warn(self, tmp_path):
        """SECRETS_MODE=warn is valid."""
        assert self._reload_with_mode(tmp_path, "warn") == "warn"

    def test_secrets_mode_off(self, tmp_path):
        """SECRETS_MODE=off is valid."""
        assert self._reload_with_mode(tmp_path, "off") == "off"

    def test_secrets_mode_invalid_falls_back(self, tmp_path):
        """Invalid SECRETS_MODE falls back to 'standard' (not 'off')."""
        assert self._reload_with_mode(tmp_path, "INVALID_VALUE") == "standard"

    def test_secrets_mode_case_insensitive(self, tmp_path):
        """SECRETS_MODE is case-insensitive."""
        assert self._reload_with_mode(tmp_path, "STRICT") == "strict"

    def test_secrets_mode_strips_whitespace(self, tmp_path):
        """SECRETS_MODE strips surrounding whitespace."""
        assert self._reload_with_mode(tmp_path, "  warn  ") == "warn"


class TestMalformedEnvNames:
    """A line whose name is not a valid identifier is reported by file and line, never by content."""

    FAKE_NAME = "fake-leaked-name-0000"
    FAKE_VALUE = "fake-value-0000"

    def test_loader_warns_with_file_and_line_and_skips_the_line(self, tmp_path, capsys):
        from hooks.config import _parse_env_file

        env_file = tmp_path / "x.env"
        env_file.write_text(f"GOOD_NAME_0000=ok\n# note\n{self.FAKE_NAME}={self.FAKE_VALUE}\n")
        with patch.dict(os.environ, {}, clear=False):
            _parse_env_file(env_file)
            assert os.environ.get(self.FAKE_NAME) is None
            assert os.environ.get("GOOD_NAME_0000") == "ok"
        err = capsys.readouterr().err
        assert f"{env_file} line 3" in err
        assert self.FAKE_NAME not in err and self.FAKE_VALUE not in err
