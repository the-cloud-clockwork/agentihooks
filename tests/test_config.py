"""Tests for hooks.config module."""

import os
import sys
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


class TestMultilineEnvValues:
    """A value spanning several lines is reported by name, never by content."""

    FAKE_FIRST = "fake-first-line-0000"
    FAKE_REST = "fake-rest=line-0000"

    def _load(self, home, monkeypatch, capsys, *, tty=True, inherited=None):
        from hooks import config

        monkeypatch.setattr(sys.stderr, "isatty", lambda: tty)
        env = {"AGENTIHOOKS_HOME": str(home), **(inherited or {})}
        with patch.dict(os.environ, env, clear=False):
            config._load_user_env()
            loaded = {k: os.environ.get(k) for k in ("GOOD_NAME_0001", "AFTER_NAME_0001")}
        return loaded, capsys.readouterr().err

    def _home(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        (home / "x.env").write_text(
            f'GOOD_NAME_0001=ok\nFAKE_MULTI_0001="{self.FAKE_FIRST}\n{self.FAKE_REST}"\nAFTER_NAME_0001=yes\n'
        )
        return home

    def test_loader_names_a_multiline_file_value_and_skips_its_continuation(self, tmp_path, monkeypatch, capsys):
        loaded, err = self._load(self._home(tmp_path), monkeypatch, capsys)
        assert loaded == {"GOOD_NAME_0001": "ok", "AFTER_NAME_0001": "yes"}
        assert "several lines" in err and "FAKE_MULTI_0001" in err
        assert "not a valid identifier" not in err
        assert self.FAKE_FIRST not in err and self.FAKE_REST not in err

    def test_loader_names_a_multiline_value_inherited_from_the_shell(self, tmp_path, monkeypatch, capsys):
        home = tmp_path / "home"
        home.mkdir()
        inherited = {"FAKE_INHERITED_0001": self.FAKE_FIRST + "\n" + self.FAKE_REST}
        _, err = self._load(home, monkeypatch, capsys, inherited=inherited)
        assert "FAKE_INHERITED_0001" in err
        assert self.FAKE_FIRST not in err and self.FAKE_REST not in err

    def test_loader_stays_silent_when_stderr_is_not_a_terminal(self, tmp_path, monkeypatch, capsys):
        _, err = self._load(self._home(tmp_path), monkeypatch, capsys, tty=False)
        assert err == ""

    def test_loader_is_silent_on_single_line_quoted_values(self, tmp_path, monkeypatch, capsys):
        home = tmp_path / "home"
        home.mkdir()
        (home / "x.env").write_text("ONE_LINE_0001=\"a b\"\nSINGLE_0001='c'\n")
        multiline = {k for k, v in os.environ.items() if "\n" in v}
        with patch.dict(os.environ, {}, clear=False):
            for k in multiline:
                del os.environ[k]
            _, err = self._load(home, monkeypatch, capsys)
        assert err == ""

    def test_loader_skips_a_value_the_shell_already_reported(self, tmp_path, monkeypatch, capsys):
        home = tmp_path / "home"
        home.mkdir()
        inherited = {
            "FAKE_INHERITED_0001": self.FAKE_FIRST + "\n" + self.FAKE_REST,
            "AGENTIHOOKS_MULTILINE_REPORTED": " FAKE_INHERITED_0001",
        }
        _, err = self._load(home, monkeypatch, capsys, inherited=inherited)
        assert "FAKE_INHERITED_0001" not in err

    @pytest.mark.parametrize(
        ("earlier", "marked"), [(None, "FAKE_MULTI_0001"), ("EARLIER_0001", "EARLIER_0001 FAKE_MULTI_0001")]
    )
    def test_loader_reports_a_value_once_and_marks_it_for_child_processes(
        self, tmp_path, monkeypatch, capsys, earlier, marked
    ):
        from hooks import config

        monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
        env = {"AGENTIHOOKS_HOME": str(self._home(tmp_path))}
        with patch.dict(os.environ, env, clear=False):
            for k in [k for k, v in os.environ.items() if "\n" in v]:
                del os.environ[k]
            os.environ.pop("AGENTIHOOKS_MULTILINE_REPORTED", None)
            if earlier:
                os.environ["AGENTIHOOKS_MULTILINE_REPORTED"] = earlier
            config._load_user_env()
            config._load_user_env()
            assert os.environ["AGENTIHOOKS_MULTILINE_REPORTED"] == marked
        err = capsys.readouterr().err
        assert err.count("[agentihooks] these values span several lines") == 1
