"""Tests for scripts/install.py — loadenv, mcp-lib, interactive uninstall."""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add scripts/ to path so we can import install directly
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import install  # noqa: I001


class TestUserEnvManifest:
    def test_install_adds_missing_defaults_without_touching_existing_values(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("# operator\nTOKEN_WARN_PCT=73\nCUSTOM=value\n")

        with (
            patch.object(install, "_ENV_FILE_DST", env_file),
            patch.object(install, "AGENTIHOOKS_ROOT", Path(__file__).parents[1]),
        ):
            added = install._seed_user_env_file()

        body = env_file.read_text()
        assert install._ENV_MANAGED_START in body
        assert install._ENV_MANAGED_END in body
        assert "# operator\nTOKEN_WARN_PCT=73\nCUSTOM=value\n" in body
        assert "TOKEN_WARN_PCT=73\n" in body
        assert body.count("TOKEN_WARN_PCT=") == 1
        assert "CUSTOM=value\n" in body
        assert "# RETRY_BREAKER_HARD_MAX=10\n" in body
        assert "# VOICE_API_KEY=\n" in body
        assert "RETRY_BREAKER_HARD_MAX" in added

    def test_second_sync_is_byte_identical(self, tmp_path):
        env_file = tmp_path / ".env"
        with (
            patch.object(install, "_ENV_FILE_DST", env_file),
            patch.object(install, "AGENTIHOOKS_ROOT", Path(__file__).parents[1]),
        ):
            install._seed_user_env_file()
            first = (env_file.read_bytes(), env_file.stat().st_mtime_ns)
            assert install._seed_user_env_file() == []
        assert (env_file.read_bytes(), env_file.stat().st_mtime_ns) == first

    def test_manifest_contains_only_agentihooks_owned_settings(self):
        with patch.object(install, "AGENTIHOOKS_ROOT", Path(__file__).parents[1]):
            defaults = install._discover_user_env_defaults()
        assert "MCP_TRANSPORT" in defaults
        assert "ALLOWED_TOOLS" in defaults
        assert "CREDENTIAL_GUARD_ENABLED" in defaults
        assert "OTEL_HOOKS_ENABLED" in defaults
        assert "VOICE_SERVICE_URL" in defaults
        assert defaults["MCP_STATELESS_HTTP"] == "false"
        assert defaults["BROADCAST_CRITICAL_ON_PRETOOL"] == "false"
        assert defaults["BROADCAST_PRETOOL_MIN_SEVERITY"] == "critical"
        foreign = {
            "AGENTIBRAIN_HOME",
            "AWS_REGION",
            "BRAIN_URL",
            "CLAUDE_MODEL",
            "CODEX_HOME",
            "COPILOT_HOME",
            "GITHUB_TOKEN",
            "KB_ROUTER_TOKEN",
            "POSTGRES_PASSWORD",
            "SMTP_PASS",
        }
        assert not foreign & set(defaults)
        assert not any(key.startswith(("BRAIN_", "AMYGDALA_", "KB_")) for key in defaults)

    def test_active_value_inside_managed_block_is_moved_to_user_overrides(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(f"{install._ENV_MANAGED_START}\nTOKEN_WARN_PCT=71\n{install._ENV_MANAGED_END}\n")

        with (
            patch.object(install, "_ENV_FILE_DST", env_file),
            patch.object(install, "AGENTIHOOKS_ROOT", Path(__file__).parents[1]),
        ):
            install._seed_user_env_file()

        body = env_file.read_text()
        managed = body.split(install._ENV_MANAGED_START, 1)[1].split(install._ENV_MANAGED_END, 1)[0]
        assert "TOKEN_WARN_PCT=" not in managed
        assert body.endswith("# User overrides\nTOKEN_WARN_PCT=71\n")

    def test_legacy_generated_catalog_is_pruned_but_active_values_survive(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# AgentiHooks user environment\n"
            "# Uncomment a setting to override the displayed program default.\n"
            "# AWS_REGION=us-east-1\n"
            "TOKEN_WARN_PCT=72\n"
        )

        with (
            patch.object(install, "_ENV_FILE_DST", env_file),
            patch.object(install, "AGENTIHOOKS_ROOT", Path(__file__).parents[1]),
        ):
            install._seed_user_env_file()

        body = env_file.read_text()
        assert "AWS_REGION" not in body
        assert body.endswith("# User overrides\nTOKEN_WARN_PCT=72\n")

    def test_duplicate_order_stays_first_definition_wins(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(f"{install._ENV_MANAGED_START}\nCUSTOM=first\n{install._ENV_MANAGED_END}\nCUSTOM=second\n")

        with (
            patch.object(install, "_ENV_FILE_DST", env_file),
            patch.object(install, "AGENTIHOOKS_ROOT", Path(__file__).parents[1]),
        ):
            install._seed_user_env_file()

        body = env_file.read_text()
        assert body.index("CUSTOM=first") < body.index("CUSTOM=second")

    def test_idempotent_sync_restores_private_mode(self, tmp_path):
        env_file = tmp_path / ".env"
        with (
            patch.object(install, "_ENV_FILE_DST", env_file),
            patch.object(install, "AGENTIHOOKS_ROOT", Path(__file__).parents[1]),
        ):
            install._seed_user_env_file()
            env_file.chmod(0o644)
            assert install._seed_user_env_file() == []

        assert env_file.stat().st_mode & 0o777 == 0o600

    def test_update_refreshes_env_manifest(self, monkeypatch):
        called = []

        monkeypatch.setattr("scripts.updater.run_update", lambda **kwargs: 0)
        monkeypatch.setattr(install, "_seed_user_env_file", lambda: called.append("env"))
        monkeypatch.setattr(install, "_load_state", lambda: {})
        monkeypatch.setattr(install, "_save_state", lambda state: None)
        monkeypatch.setattr(install, "_is_source_checkout", lambda: False)
        monkeypatch.setattr(sys, "argv", ["agentihooks", "update"])

        with pytest.raises(SystemExit) as exc:
            install.main()

        assert exc.value.code == 0
        assert called == ["env"]


class TestInitTargets:
    def test_bare_init_installs_all_supported_targets(self, monkeypatch):
        calls = []
        monkeypatch.setattr(install, "cmd_init_unified", lambda args: calls.append(args))
        monkeypatch.setattr(sys, "argv", ["agentihooks", "init", "--profile", "default"])

        install.main()

        assert [args.install_target for args in calls] == list(install.SUPPORTED_TARGETS)

    def test_target_flag_installs_only_that_target(self, monkeypatch):
        calls = []
        monkeypatch.setattr(install, "cmd_init_unified", lambda args: calls.append(args))
        monkeypatch.setattr(
            sys,
            "argv",
            ["agentihooks", "init", "--profile", "default", "--target", "codex"],
        )

        install.main()

        assert [args.install_target for args in calls] == ["codex"]

    def test_target_environment_installs_only_that_target(self, monkeypatch):
        calls = []
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "copilot")
        monkeypatch.setattr(install, "cmd_init_unified", lambda args: calls.append(args))
        monkeypatch.setattr(sys, "argv", ["agentihooks", "init", "--profile", "default"])

        install.main()

        assert [args.install_target for args in calls] == ["copilot"]


# ---------------------------------------------------------------------------
# _cmd_loadenv / bashrc block management
# ---------------------------------------------------------------------------


class TestLoadenvBashrcBlock:
    def test_adds_block_when_absent(self, tmp_path):
        bashrc = tmp_path / ".bashrc"
        env_file = tmp_path / ".env"
        env_file.write_text("KEY=val\n")

        with (
            patch.object(install, "_BASHRC", bashrc),
            patch.object(install, "_ENV_FILE_DST", env_file),
            patch.object(install, "_prompt_install_requirements"),
        ):
            install._cmd_loadenv(env_file, [])

        content = bashrc.read_text()
        assert "# === agentihooks ===" in content
        assert "agentienv()" in content
        assert str(env_file) in content
        # Function is defined AND auto-called
        assert "\nagentienv\n" in content
        assert "# === end-agentihooks ===" in content

    def test_replaces_existing_block(self, tmp_path):
        bashrc = tmp_path / ".bashrc"
        env_file = tmp_path / ".env"
        env_file.write_text("KEY=val\n")
        bashrc.write_text(
            "# before\n"
            "# === agentihooks ===\n"
            "agentienv() { set -a; . /old/path/.env; set +a; }\n"
            "# === end-agentihooks ===\n"
            "# after\n"
        )

        with (
            patch.object(install, "_BASHRC", bashrc),
            patch.object(install, "_ENV_FILE_DST", env_file),
            patch.object(install, "_prompt_install_requirements"),
        ):
            install._cmd_loadenv(env_file, [])

        content = bashrc.read_text()
        assert content.count("# === agentihooks ===") == 1
        assert str(env_file) in content
        assert "/old/path" not in content
        assert "# before" in content
        assert "# after" in content

    def test_idempotent_double_run(self, tmp_path):
        bashrc = tmp_path / ".bashrc"
        env_file = tmp_path / ".env"
        env_file.write_text("KEY=val\n")

        with (
            patch.object(install, "_BASHRC", bashrc),
            patch.object(install, "_ENV_FILE_DST", env_file),
            patch.object(install, "_prompt_install_requirements"),
        ):
            install._cmd_loadenv(env_file, [])
            install._cmd_loadenv(env_file, [])

        content = bashrc.read_text()
        assert content.count("# === agentihooks ===") == 1

    def test_exits_when_env_file_missing(self, tmp_path):
        missing = tmp_path / "no.env"
        with pytest.raises(SystemExit) as exc:
            install._cmd_loadenv(missing, [])
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# _find_requirements_files
# ---------------------------------------------------------------------------


class TestFindRequirementsFiles:
    def test_finds_in_state_dir(self, tmp_path):
        req = tmp_path / "requirements.txt"
        req.write_text("requests\n")
        with patch.object(install, "AGENTIHOOKS_STATE_DIR", tmp_path):
            with patch.object(install, "_state_get_mcp_lib", return_value=None):
                found = install._find_requirements_files()
        assert req in found

    def test_finds_in_mcp_lib_path(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        req = lib / "requirements.txt"
        req.write_text("boto3\n")
        with patch.object(install, "AGENTIHOOKS_STATE_DIR", tmp_path):
            with patch.object(install, "_state_get_mcp_lib", return_value=lib):
                found = install._find_requirements_files()
        assert req in found

    def test_deduplicates_same_path(self, tmp_path):
        req = tmp_path / "requirements.txt"
        req.write_text("requests\n")
        with patch.object(install, "AGENTIHOOKS_STATE_DIR", tmp_path):
            with patch.object(install, "_state_get_mcp_lib", return_value=tmp_path):
                found = install._find_requirements_files()
        assert found.count(req) == 1

    def test_returns_empty_when_none(self, tmp_path):
        with patch.object(install, "AGENTIHOOKS_STATE_DIR", tmp_path):
            with patch.object(install, "_state_get_mcp_lib", return_value=None):
                found = install._find_requirements_files()
        assert found == []


# ---------------------------------------------------------------------------
# _detect_venv
# ---------------------------------------------------------------------------


class TestDetectVenv:
    def test_detects_via_virtual_env_var(self, tmp_path):
        python = tmp_path / "bin" / "python"
        python.parent.mkdir()
        python.touch()
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        with patch("pathlib.Path.home", return_value=fake_home):
            with patch.dict("os.environ", {"VIRTUAL_ENV": str(tmp_path)}, clear=True):
                result = install._detect_venv()
        assert result == python

    @staticmethod
    def _sandbox_root(monkeypatch, tmp_path):
        """Point AGENTIHOOKS_ROOT at a scratch tree.

        Tier 3 reads AGENTIHOOKS_ROOT and its parent. Left unsandboxed these
        tests reach the operator's real workspace ``.venv``, which exists and
        passes the import probe, so they would assert against live state.
        """
        root = tmp_path / "workspace" / "repo"
        root.mkdir(parents=True)
        monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", root)
        return root

    def test_detects_local_venv(self, tmp_path, monkeypatch):
        self._sandbox_root(monkeypatch, tmp_path)
        venv = tmp_path / ".venv" / "bin" / "python"
        venv.parent.mkdir(parents=True)
        venv.touch()
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        with patch("pathlib.Path.home", return_value=fake_home):
            with patch("pathlib.Path.cwd", return_value=tmp_path):
                with patch.dict("os.environ", {}, clear=True):
                    result = install._detect_venv()
        assert result == venv

    def test_agentihooks_python_pin_wins(self, tmp_path):
        pinned = tmp_path / "pinned" / "python"
        pinned.parent.mkdir()
        pinned.touch()
        other = tmp_path / "activated"
        (other / "bin").mkdir(parents=True)
        (other / "bin" / "python").touch()
        env = {"AGENTIHOOKS_PYTHON": str(pinned), "VIRTUAL_ENV": str(other)}
        with patch.dict("os.environ", env, clear=True):
            assert install._detect_venv() == pinned

    def test_pin_is_read_from_agentihooks_env_files(self, tmp_path, monkeypatch):
        """The installer never imports hooks.config, so a pin written to
        ~/.agentihooks/*.env must be read explicitly or it silently does
        nothing in a fresh shell."""
        pinned = tmp_path / "pinned" / "python"
        pinned.parent.mkdir()
        pinned.touch()
        monkeypatch.setattr(
            install,
            "_mcp_daemon_module",
            lambda: type(
                "M", (), {"_scan_env_file": staticmethod(lambda k: str(pinned) if k == "AGENTIHOOKS_PYTHON" else "")}
            ),
        )
        with patch.dict("os.environ", {}, clear=True):
            assert install._detect_venv() == pinned

    def test_env_pin_beats_repo_venv(self, tmp_path, monkeypatch):
        pinned = tmp_path / "wanted" / "python"
        pinned.parent.mkdir()
        pinned.touch()
        root = self._sandbox_root(monkeypatch, tmp_path)
        repo_venv = root / ".venv" / "bin" / "python"
        repo_venv.parent.mkdir(parents=True, exist_ok=True)
        repo_venv.touch()
        monkeypatch.setattr(
            install,
            "_mcp_daemon_module",
            lambda: type("M", (), {"_scan_env_file": staticmethod(lambda k: str(pinned))}),
        )
        with patch.dict("os.environ", {}, clear=True):
            assert install._detect_venv() == pinned

    def test_detects_repo_root_venv(self, tmp_path, monkeypatch):
        root = self._sandbox_root(monkeypatch, tmp_path)
        root_venv = root / ".venv" / "bin" / "python"
        root_venv.parent.mkdir(parents=True, exist_ok=True)
        root_venv.touch()
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        with patch("pathlib.Path.home", return_value=fake_home):
            with patch("pathlib.Path.cwd", return_value=tmp_path):
                with patch.dict("os.environ", {}, clear=True):
                    assert install._detect_venv() == root_venv

    def test_workspace_venv_beats_repo_local_venv(self, tmp_path, monkeypatch):
        """The workspace venv is the one hooks run under; a repo-local .venv
        (`uv run` leaves one behind) must not outrank it — both can import
        hooks, so the probe cannot separate them and the order decides."""
        root = self._sandbox_root(monkeypatch, tmp_path)
        repo_venv = root / ".venv" / "bin" / "python"
        repo_venv.parent.mkdir(parents=True)
        repo_venv.touch()
        workspace_venv = root.parent / ".venv" / "bin" / "python"
        workspace_venv.parent.mkdir(parents=True)
        workspace_venv.touch()
        monkeypatch.setattr(install, "_python_can_import_hooks", lambda p: True)
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        with patch("pathlib.Path.home", return_value=fake_home):
            with patch("pathlib.Path.cwd", return_value=tmp_path):
                with patch.dict("os.environ", {}, clear=True):
                    assert install._detect_venv() == workspace_venv

    def test_unimportable_workspace_venv_loses_to_repo_local(self, tmp_path, monkeypatch):
        """The probe is load-bearing for fallthrough, not decoration."""
        root = self._sandbox_root(monkeypatch, tmp_path)
        repo_venv = root / ".venv" / "bin" / "python"
        repo_venv.parent.mkdir(parents=True)
        repo_venv.touch()
        workspace_venv = root.parent / ".venv" / "bin" / "python"
        workspace_venv.parent.mkdir(parents=True)
        workspace_venv.touch()
        monkeypatch.setattr(install, "_python_can_import_hooks", lambda p: Path(p) == repo_venv)
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        with patch("pathlib.Path.home", return_value=fake_home):
            with patch("pathlib.Path.cwd", return_value=tmp_path):
                with patch.dict("os.environ", {}, clear=True):
                    assert install._detect_venv() == repo_venv

    def test_state_dir_venv_is_never_used(self, tmp_path, monkeypatch):
        """~/.agentihooks/.venv must NEVER be picked up (operator directive)."""
        self._sandbox_root(monkeypatch, tmp_path)
        fake_home = tmp_path / "home"
        state_venv = fake_home / ".agentihooks" / ".venv" / "bin" / "python"
        state_venv.parent.mkdir(parents=True)
        state_venv.touch()
        with patch("pathlib.Path.home", return_value=fake_home):
            with patch("pathlib.Path.cwd", return_value=tmp_path):
                with patch.dict("os.environ", {}, clear=True):
                    assert install._detect_venv() is None

    def test_returns_none_when_no_venv(self, tmp_path, monkeypatch):
        self._sandbox_root(monkeypatch, tmp_path)
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        with patch("pathlib.Path.home", return_value=fake_home):
            with patch("pathlib.Path.cwd", return_value=tmp_path):
                with patch.dict("os.environ", {}, clear=True):
                    result = install._detect_venv()
        assert result is None


# ---------------------------------------------------------------------------
# _deep_merge
# ---------------------------------------------------------------------------


class TestDeepMerge:
    def test_simple_values_override_wins(self):
        base = {"model": "sonnet", "flag": True}
        override = {"model": "opus"}
        result = install._deep_merge(base, override)
        assert result["model"] == "opus"
        assert result["flag"] is True

    def test_dicts_merge_key_by_key(self):
        base = {"env": {"A": "1", "B": "2"}}
        override = {"env": {"B": "99", "C": "3"}}
        result = install._deep_merge(base, override)
        assert result["env"] == {"A": "1", "B": "99", "C": "3"}

    def test_hooks_arrays_append(self):
        base = {
            "hooks": {
                "PreToolUse": [{"hooks": [{"command": "python -m hooks"}]}],
                "Stop": [{"hooks": [{"command": "python -m hooks"}]}],
            }
        }
        override = {
            "hooks": {
                "PreToolUse": [{"hooks": [{"command": "my-linter.sh"}]}],
            }
        }
        result = install._deep_merge(base, override)
        # PreToolUse: base + profile appended
        assert len(result["hooks"]["PreToolUse"]) == 2
        assert result["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "python -m hooks"
        assert result["hooks"]["PreToolUse"][1]["hooks"][0]["command"] == "my-linter.sh"
        # Stop: untouched
        assert len(result["hooks"]["Stop"]) == 1

    def test_permissions_allow_replaced(self):
        base = {"permissions": {"allow": ["Bash(*)", "Read(*)"]}}
        override = {"permissions": {"allow": ["Bash(*)"]}}
        result = install._deep_merge(base, override)
        assert result["permissions"]["allow"] == ["Bash(*)"]

    def test_non_hook_arrays_replaced(self):
        base = {"other": [1, 2, 3]}
        override = {"other": [4, 5]}
        result = install._deep_merge(base, override)
        assert result["other"] == [4, 5]

    def test_hooks_new_event_added(self):
        base = {"hooks": {"Stop": [{"hooks": [{"command": "base"}]}]}}
        override = {"hooks": {"NewEvent": [{"hooks": [{"command": "new"}]}]}}
        result = install._deep_merge(base, override)
        assert "Stop" in result["hooks"]
        assert "NewEvent" in result["hooks"]

    def test_base_not_mutated(self):
        base = {"hooks": {"PreToolUse": [{"hooks": [{"command": "base"}]}]}}
        override = {"hooks": {"PreToolUse": [{"hooks": [{"command": "extra"}]}]}}
        install._deep_merge(base, override)
        assert len(base["hooks"]["PreToolUse"]) == 1


# ---------------------------------------------------------------------------
# _prompt_install_requirements
# ---------------------------------------------------------------------------


class TestPromptInstallRequirements:
    def test_skips_when_no_files(self, tmp_path):
        with patch.object(install, "_find_requirements_files", return_value=[]):
            install._prompt_install_requirements()  # should not raise

    def test_skips_when_uv_missing(self, tmp_path, capsys):
        req = tmp_path / "requirements.txt"
        req.write_text("requests\n")
        with (
            patch.object(install, "_find_requirements_files", return_value=[req]),
            patch("shutil.which", return_value=None),
        ):
            install._prompt_install_requirements()
        assert "uv not found" in capsys.readouterr().out

    def test_skips_on_n_answer(self, tmp_path, capsys):
        req = tmp_path / "requirements.txt"
        req.write_text("requests\n")
        with (
            patch.object(install, "_find_requirements_files", return_value=[req]),
            patch("shutil.which", return_value="/usr/bin/uv"),
            patch("builtins.input", return_value="n"),
        ):
            install._prompt_install_requirements()
        assert "Skipped" in capsys.readouterr().out

    def test_no_venv_prints_instructions(self, tmp_path, capsys):
        req = tmp_path / "requirements.txt"
        req.write_text("requests\n")
        with (
            patch.object(install, "_find_requirements_files", return_value=[req]),
            patch("shutil.which", return_value="/usr/bin/uv"),
            patch("builtins.input", return_value="y"),
            patch.object(install, "_detect_venv", return_value=None),
        ):
            install._prompt_install_requirements()
        out = capsys.readouterr().out
        assert "No virtual environment" in out
        assert "--force" in out

    def test_force_uses_sys_executable(self, tmp_path):
        req = tmp_path / "requirements.txt"
        req.write_text("requests\n")
        run_mock = MagicMock(returncode=0)
        with (
            patch.object(install, "_find_requirements_files", return_value=[req]),
            patch("shutil.which", return_value="/usr/bin/uv"),
            patch("builtins.input", return_value="y"),
            patch("subprocess.run", return_value=run_mock) as sub,
        ):
            install._prompt_install_requirements(force=True)
        args = sub.call_args[0][0]
        assert args[0] == "/usr/bin/uv"
        assert "--python" in args
        assert str(sys.executable) in args

    def test_eof_on_prompt_skips(self, tmp_path, capsys):
        req = tmp_path / "requirements.txt"
        req.write_text("requests\n")
        with (
            patch.object(install, "_find_requirements_files", return_value=[req]),
            patch("shutil.which", return_value="/usr/bin/uv"),
            patch("builtins.input", side_effect=EOFError),
        ):
            install._prompt_install_requirements()
        assert "Skipped" in capsys.readouterr().out

    def test_venv_install_success(self, tmp_path, capsys):
        req = tmp_path / "requirements.txt"
        req.write_text("requests\n")
        venv_python = tmp_path / ".venv" / "bin" / "python"
        run_mock = MagicMock(returncode=0)
        with (
            patch.object(install, "_find_requirements_files", return_value=[req]),
            patch("shutil.which", return_value="/usr/bin/uv"),
            patch("builtins.input", return_value="y"),
            patch.object(install, "_detect_venv", return_value=venv_python),
            patch("subprocess.run", return_value=run_mock),
        ):
            install._prompt_install_requirements()
        assert "Installed" in capsys.readouterr().out

    def test_uv_install_failure_prints_error(self, tmp_path, capsys):
        req = tmp_path / "requirements.txt"
        req.write_text("requests\n")
        venv_python = tmp_path / ".venv" / "bin" / "python"
        run_mock = MagicMock(returncode=1)
        with (
            patch.object(install, "_find_requirements_files", return_value=[req]),
            patch("shutil.which", return_value="/usr/bin/uv"),
            patch("builtins.input", return_value="y"),
            patch.object(install, "_detect_venv", return_value=venv_python),
            patch("subprocess.run", return_value=run_mock),
        ):
            install._prompt_install_requirements()
        assert "failed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _state_set_mcp_lib / _state_get_mcp_lib
# ---------------------------------------------------------------------------


class TestMcpLibState:
    def test_round_trip(self, tmp_path):
        state_json = tmp_path / "state.json"
        with (
            patch.object(install, "STATE_JSON", state_json),
            patch.object(install, "AGENTIHOOKS_STATE_DIR", tmp_path),
        ):
            install._state_set_mcp_lib(Path("/some/dir"))
            result = install._state_get_mcp_lib()
        assert result == Path("/some/dir")

    def test_returns_none_when_not_set(self, tmp_path):
        state_json = tmp_path / "state.json"
        with patch.object(install, "STATE_JSON", state_json):
            result = install._state_get_mcp_lib()
        assert result is None


# ---------------------------------------------------------------------------
# _cmd_mcp_lib
# ---------------------------------------------------------------------------


class TestCmdMcpLib:
    def _make_mcp_file(self, directory: Path, name: str, servers: dict) -> Path:
        f = directory / name
        f.write_text(json.dumps({"mcpServers": servers}))
        return f

    def test_exits_when_no_saved_path_and_none_given(self, tmp_path):
        with (
            patch.object(install, "_state_get_mcp_lib", return_value=None),
            pytest.raises(SystemExit) as exc,
        ):
            install._cmd_mcp_lib(None)
        assert exc.value.code == 1

    def test_exits_when_dir_not_found(self, tmp_path):
        missing = tmp_path / "nope"
        with pytest.raises(SystemExit):
            install._cmd_mcp_lib(missing)

    def test_exits_when_no_mcp_files(self, tmp_path):
        (tmp_path / "empty.json").write_text('{"other": {}}')
        with pytest.raises(SystemExit) as exc:
            install._cmd_mcp_lib(tmp_path)
        assert exc.value.code == 0

    def test_lists_files_and_installs_selection(self, tmp_path):
        self._make_mcp_file(tmp_path, "a.json", {"server-a": {}})
        self._make_mcp_file(tmp_path, "b.json", {"server-b": {}})

        with (
            patch.object(install, "_state_set_mcp_lib"),
            patch.object(install, "_load_state", return_value={"mcpFiles": []}),
            patch("builtins.input", return_value="1"),
            patch.object(install, "manage_user_mcp") as mock_install,
        ):
            install._cmd_mcp_lib(tmp_path)

        mock_install.assert_called_once()

    def test_q_aborts(self, tmp_path):
        self._make_mcp_file(tmp_path, "a.json", {"server-a": {}})
        with (
            patch.object(install, "_state_set_mcp_lib"),
            patch.object(install, "_load_state", return_value={"mcpFiles": []}),
            patch("builtins.input", return_value="q"),
            pytest.raises(SystemExit) as exc,
        ):
            install._cmd_mcp_lib(tmp_path)
        assert exc.value.code == 0

    def test_uses_saved_path_when_none_given(self, tmp_path, capsys):
        self._make_mcp_file(tmp_path, "a.json", {"server-a": {}})
        with (
            patch.object(install, "_state_get_mcp_lib", return_value=tmp_path),
            patch.object(install, "_state_set_mcp_lib"),
            patch.object(install, "_load_state", return_value={"mcpFiles": []}),
            patch("builtins.input", return_value="q"),
            pytest.raises(SystemExit),
        ):
            install._cmd_mcp_lib(None)
        assert "Using saved MCP library" in capsys.readouterr().out

    def test_skips_unreadable_json(self, tmp_path):
        (tmp_path / "bad.json").write_text("not json{{{")
        self._make_mcp_file(tmp_path, "good.json", {"server-a": {}})
        with (
            patch.object(install, "_state_set_mcp_lib"),
            patch.object(install, "_load_state", return_value={"mcpFiles": []}),
            patch("builtins.input", return_value="1"),
            patch.object(install, "manage_user_mcp"),
        ):
            install._cmd_mcp_lib(tmp_path)  # should not raise

    def test_eof_aborts(self, tmp_path):
        self._make_mcp_file(tmp_path, "a.json", {"server-a": {}})
        with (
            patch.object(install, "_state_set_mcp_lib"),
            patch.object(install, "_load_state", return_value={"mcpFiles": []}),
            patch("builtins.input", side_effect=EOFError),
            pytest.raises(SystemExit) as exc,
        ):
            install._cmd_mcp_lib(tmp_path)
        assert exc.value.code == 0

    def test_invalid_selection_exits_1(self, tmp_path):
        self._make_mcp_file(tmp_path, "a.json", {"server-a": {}})
        with (
            patch.object(install, "_state_set_mcp_lib"),
            patch.object(install, "_load_state", return_value={"mcpFiles": []}),
            patch("builtins.input", return_value="99"),
            pytest.raises(SystemExit) as exc,
        ):
            install._cmd_mcp_lib(tmp_path)
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# _cmd_mcp_interactive_uninstall
# ---------------------------------------------------------------------------


class TestInteractiveUninstall:
    def test_exits_when_no_tracked_files(self, tmp_path, capsys):
        with patch.object(install, "_load_state", return_value={"mcpFiles": []}):
            with patch.object(install, "STATE_JSON", tmp_path / "state.json"):
                install._cmd_mcp_interactive_uninstall()
        assert "nothing to uninstall" in capsys.readouterr().out

    def test_uninstalls_selected_file(self, tmp_path):
        mcp = tmp_path / "test.json"
        mcp.write_text(json.dumps({"mcpServers": {"srv": {}}}))
        with (
            patch.object(install, "_load_state", return_value={"mcpFiles": [str(mcp)]}),
            patch("builtins.input", return_value="1"),
            patch.object(install, "manage_user_mcp") as mock_uninstall,
        ):
            install._cmd_mcp_interactive_uninstall()
        mock_uninstall.assert_called_once_with(mcp, uninstall=True)

    def test_invalid_selection_exits_1(self, tmp_path):
        mcp = tmp_path / "test.json"
        mcp.write_text(json.dumps({"mcpServers": {"srv": {}}}))
        with (
            patch.object(install, "_load_state", return_value={"mcpFiles": [str(mcp)]}),
            patch("builtins.input", return_value="99"),
            pytest.raises(SystemExit) as exc,
        ):
            install._cmd_mcp_interactive_uninstall()
        assert exc.value.code == 1

    def test_shows_file_not_found_label(self, tmp_path, capsys):
        missing = str(tmp_path / "gone.json")
        with (
            patch.object(install, "_load_state", return_value={"mcpFiles": [missing]}),
            patch("builtins.input", return_value="1"),
            patch.object(install, "manage_user_mcp"),
        ):
            install._cmd_mcp_interactive_uninstall()
        assert "file not found" in capsys.readouterr().out

    def test_shows_unreadable_label(self, tmp_path, capsys):
        mcp = tmp_path / "bad.json"
        mcp.write_text("not json{{{")
        with (
            patch.object(install, "_load_state", return_value={"mcpFiles": [str(mcp)]}),
            patch("builtins.input", return_value="1"),
            patch.object(install, "manage_user_mcp"),
        ):
            install._cmd_mcp_interactive_uninstall()
        assert "unreadable" in capsys.readouterr().out

    def test_q_aborts(self, tmp_path):
        mcp = tmp_path / "test.json"
        mcp.write_text(json.dumps({"mcpServers": {"srv": {}}}))
        with (
            patch.object(install, "_load_state", return_value={"mcpFiles": [str(mcp)]}),
            patch("builtins.input", return_value="q"),
            pytest.raises(SystemExit) as exc,
        ):
            install._cmd_mcp_interactive_uninstall()
        assert exc.value.code == 0

    def test_eof_aborts(self, tmp_path):
        mcp = tmp_path / "test.json"
        mcp.write_text(json.dumps({"mcpServers": {"srv": {}}}))
        with (
            patch.object(install, "_load_state", return_value={"mcpFiles": [str(mcp)]}),
            patch("builtins.input", side_effect=EOFError),
            pytest.raises(SystemExit) as exc,
        ):
            install._cmd_mcp_interactive_uninstall()
        assert exc.value.code == 0


# ---------------------------------------------------------------------------
# Init idempotency — profile recall from state.json
# ---------------------------------------------------------------------------


class TestInitProfileRecall:
    """Verify that init reuses stored profile/settings_profile from state.json."""

    def _make_state(self, profile, settings_profile=""):
        entry = {"path": "/home/test/.claude", "profile": profile, "installed_at": "2026-01-01T00:00:00Z"}
        if settings_profile:
            entry["settings_profile"] = settings_profile
        return {"targets": {"global": entry}}

    def test_recalls_profile_from_state(self):
        """When no CLI flag or env var, init uses profile from state.json."""
        import argparse

        state = self._make_state("anton")
        args = argparse.Namespace(
            profile=None,
            init_settings_profile=None,
            bundle=None,
            repo=None,
            query=False,
            list_profiles=False,
        )
        with (
            patch.object(install, "_load_state", return_value=state),
            patch.object(install, "_get_bundle_path", return_value=None),
            patch.object(install, "install_global") as mock_install,
            patch.dict("os.environ", {}, clear=False),
        ):
            # Remove AGENTIHOOKS_PROFILE if set
            import os

            os.environ.pop("AGENTIHOOKS_PROFILE", None)
            install.cmd_init_unified(args)
            called_args = mock_install.call_args[0][0]
            assert called_args.profile == "anton"

    def test_recalls_settings_profile_from_state(self):
        """When no CLI flag or env var, init uses settings_profile from state.json."""
        import argparse

        state = self._make_state("anton", settings_profile="admin")
        args = argparse.Namespace(
            profile="anton",
            init_settings_profile=None,
            bundle=None,
            repo=None,
            query=False,
            list_profiles=False,
        )
        with (
            patch.object(install, "_load_state", return_value=state),
            patch.object(install, "_get_bundle_path", return_value=None),
            patch.object(install, "install_global") as mock_install,
            patch.dict("os.environ", {}, clear=False),
        ):
            import os

            os.environ.pop("AGENTIHOOKS_SETTINGS_PROFILE", None)
            install.cmd_init_unified(args)
            called_args = mock_install.call_args[0][0]
            assert called_args.settings_profile == "admin"

    def test_cli_flag_overrides_state(self):
        """CLI --profile overrides state.json stored profile."""
        import argparse

        state = self._make_state("default")
        args = argparse.Namespace(
            profile="anton",
            init_settings_profile=None,
            bundle=None,
            repo=None,
            query=False,
            list_profiles=False,
        )
        with (
            patch.object(install, "_load_state", return_value=state),
            patch.object(install, "_get_bundle_path", return_value=None),
            patch.object(install, "install_global") as mock_install,
            patch.dict("os.environ", {}, clear=False),
        ):
            install.cmd_init_unified(args)
            called_args = mock_install.call_args[0][0]
            assert called_args.profile == "anton"

    def test_env_var_overrides_state(self):
        """AGENTIHOOKS_PROFILE env var overrides state.json."""
        import argparse

        state = self._make_state("default")
        args = argparse.Namespace(
            profile=None,
            init_settings_profile=None,
            bundle=None,
            repo=None,
            query=False,
            list_profiles=False,
        )
        with (
            patch.object(install, "_load_state", return_value=state),
            patch.object(install, "_get_bundle_path", return_value=None),
            patch.object(install, "install_global") as mock_install,
            patch.dict("os.environ", {"AGENTIHOOKS_PROFILE": "admin"}, clear=False),
        ):
            install.cmd_init_unified(args)
            called_args = mock_install.call_args[0][0]
            assert called_args.profile == "admin"

    def test_prompts_when_no_state(self):
        """When state.json has no profile, falls back to interactive prompt."""
        import argparse

        args = argparse.Namespace(
            profile=None,
            init_settings_profile=None,
            bundle=None,
            repo=None,
            query=False,
            list_profiles=False,
        )
        with (
            patch.object(install, "_load_state", return_value={}),
            patch.object(install, "_get_bundle_path", return_value=None),
            patch.object(install, "_available_profiles", return_value=["default", "anton"]),
            patch.object(install, "install_global") as mock_install,
            patch("builtins.input", return_value="anton"),
            patch("sys.stdin") as mock_stdin,
            patch.dict("os.environ", {}, clear=False),
        ):
            import os

            os.environ.pop("AGENTIHOOKS_PROFILE", None)
            mock_stdin.isatty.return_value = True
            install.cmd_init_unified(args)
            called_args = mock_install.call_args[0][0]
            assert called_args.profile == "anton"


# ---------------------------------------------------------------------------
# query_active_profile — settings_profile reporting
# ---------------------------------------------------------------------------


class TestQueryActiveProfile:
    def test_shows_settings_profile(self, capsys):
        state = {
            "targets": {
                "global": {
                    "path": "/home/test/.claude",
                    "profile": "anton",
                    "settings_profile": "admin",
                    "installed_at": "2026-01-01T00:00:00Z",
                },
            },
        }
        with (
            patch.object(install, "_load_state", return_value=state),
            patch("pathlib.Path.exists", return_value=False),  # no local .agentihooks.json
        ):
            install.query_active_profile()
        out = capsys.readouterr().out
        assert "anton" in out
        assert "settings: admin" in out

    def test_no_settings_profile_no_line(self, capsys):
        state = {
            "targets": {
                "global": {
                    "path": "/home/test/.claude",
                    "profile": "anton",
                    "installed_at": "2026-01-01T00:00:00Z",
                },
            },
        }
        with (
            patch.object(install, "_load_state", return_value=state),
            patch("pathlib.Path.exists", return_value=False),
        ):
            install.query_active_profile()
        out = capsys.readouterr().out
        assert "anton" in out
        assert "settings:" not in out


# ---------------------------------------------------------------------------
# link-profile feature
# ---------------------------------------------------------------------------


class TestLinkProfile:
    """Tests for `agentihooks link-profile <path>` and friends."""

    def _setup(self, tmp_path: Path):
        """Create a fixture with a fresh STATE_JSON, CLAUDE_HOME, PROFILES_DIR, no bundle."""
        state_json = tmp_path / "state.json"
        claude_home = tmp_path / ".claude"
        claude_home.mkdir()
        for sub in ("rules", "agents", "commands", "skills"):
            (claude_home / sub).mkdir()
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        # Pre-existing built-in 'anton' fixture
        (profiles_dir / "anton").mkdir()
        return state_json, claude_home, profiles_dir

    # --- _resolve_profile_dir tier ordering ---

    def test_resolver_prefers_builtin_over_linked(self, tmp_path):
        state_json, _, profiles_dir = self._setup(tmp_path)
        external = tmp_path / "external" / "anton"
        external.mkdir(parents=True)
        state_json.write_text(json.dumps({"linked_profiles": [{"name": "anton", "path": str(external)}]}))
        with (
            patch.object(install, "STATE_JSON", state_json),
            patch.object(install, "PROFILES_DIR", profiles_dir),
            patch.object(install, "_get_bundle_path", return_value=None),
        ):
            resolved = install._resolve_profile_dir("anton")
        assert resolved == profiles_dir / "anton"

    def test_resolver_falls_through_to_linked(self, tmp_path):
        state_json, _, profiles_dir = self._setup(tmp_path)
        external = tmp_path / "external" / "brain"
        external.mkdir(parents=True)
        state_json.write_text(json.dumps({"linked_profiles": [{"name": "brain", "path": str(external)}]}))
        with (
            patch.object(install, "STATE_JSON", state_json),
            patch.object(install, "PROFILES_DIR", profiles_dir),
            patch.object(install, "_get_bundle_path", return_value=None),
        ):
            resolved = install._resolve_profile_dir("brain")
        assert resolved == external

    def test_profile_source_label(self, tmp_path):
        state_json, _, profiles_dir = self._setup(tmp_path)
        external = tmp_path / "external" / "brain"
        external.mkdir(parents=True)
        state_json.write_text(json.dumps({"linked_profiles": [{"name": "brain", "path": str(external)}]}))
        with (
            patch.object(install, "STATE_JSON", state_json),
            patch.object(install, "PROFILES_DIR", profiles_dir),
            patch.object(install, "_get_bundle_path", return_value=None),
        ):
            assert install._profile_source_label("anton") == "built-in"
            assert install._profile_source_label("brain") == "linked"
            assert install._profile_source_label("nope") == "unknown"

    # --- collision guard ---

    def test_collision_with_builtin_rejects(self, tmp_path):
        state_json, _, profiles_dir = self._setup(tmp_path)
        external = tmp_path / "external" / "anton"
        external.mkdir(parents=True)
        with (
            patch.object(install, "STATE_JSON", state_json),
            patch.object(install, "PROFILES_DIR", profiles_dir),
            patch.object(install, "_get_bundle_path", return_value=None),
            pytest.raises(SystemExit) as exc,
        ):
            install._link_profile_link(external, name=None, append=True, run_init=False)
        assert exc.value.code == 1

    # --- happy path link ---

    def test_link_appends_to_chain_and_writes_state(self, tmp_path):
        state_json, _, profiles_dir = self._setup(tmp_path)
        external = tmp_path / "external" / "brain"
        external.mkdir(parents=True)
        state_json.write_text(json.dumps({"targets": {"global": {"profile": "anton", "settings_profile": ""}}}))
        with (
            patch.object(install, "STATE_JSON", state_json),
            patch.object(install, "PROFILES_DIR", profiles_dir),
            patch.object(install, "_get_bundle_path", return_value=None),
        ):
            install._link_profile_link(external, name=None, append=True, run_init=False)
        result = json.loads(state_json.read_text())
        assert result["targets"]["global"]["claude"]["profile"] == "anton,brain"
        assert result["linked_profiles"][0]["name"] == "brain"
        assert result["linked_profiles"][0]["path"] == str(external)
        # --no-init should bump installed_at
        assert "installed_at" in result["targets"]["global"]["claude"]

    def test_link_idempotent_re_link(self, tmp_path):
        state_json, _, profiles_dir = self._setup(tmp_path)
        external = tmp_path / "external" / "brain"
        external.mkdir(parents=True)
        state_json.write_text(
            json.dumps(
                {
                    "targets": {"global": {"profile": "anton,brain"}},
                    "linked_profiles": [{"name": "brain", "path": str(external)}],
                }
            )
        )
        with (
            patch.object(install, "STATE_JSON", state_json),
            patch.object(install, "PROFILES_DIR", profiles_dir),
            patch.object(install, "_get_bundle_path", return_value=None),
        ):
            install._link_profile_link(external, name=None, append=True, run_init=False)
        result = json.loads(state_json.read_text())
        # No duplicate in chain
        assert result["targets"]["global"]["claude"]["profile"] == "anton,brain"
        assert len(result["linked_profiles"]) == 1

    # --- unlink ---

    def test_unlink_strips_chain(self, tmp_path):
        state_json, _, profiles_dir = self._setup(tmp_path)
        external = tmp_path / "external" / "brain"
        external.mkdir(parents=True)
        state_json.write_text(
            json.dumps(
                {
                    "targets": {"global": {"profile": "anton,brain"}},
                    "linked_profiles": [{"name": "brain", "path": str(external)}],
                }
            )
        )
        with (
            patch.object(install, "STATE_JSON", state_json),
            patch.object(install, "CLAUDE_HOME", tmp_path / ".claude"),
            patch.object(install, "_available_profiles", return_value=["anton", "default"]),
        ):
            install._link_profile_unlink("brain", run_init=False)
        result = json.loads(state_json.read_text())
        assert result["targets"]["global"]["claude"]["profile"] == "anton"
        assert result["linked_profiles"] == []

    def test_unlink_empty_chain_falls_back_to_default(self, tmp_path):
        state_json, _, profiles_dir = self._setup(tmp_path)
        external = tmp_path / "external" / "brain"
        external.mkdir(parents=True)
        state_json.write_text(
            json.dumps(
                {
                    "targets": {"global": {"profile": "brain"}},
                    "linked_profiles": [{"name": "brain", "path": str(external)}],
                }
            )
        )
        with (
            patch.object(install, "STATE_JSON", state_json),
            patch.object(install, "CLAUDE_HOME", tmp_path / ".claude"),
            patch.object(install, "_available_profiles", return_value=["anton", "default"]),
        ):
            install._link_profile_unlink("brain", run_init=False)
        result = json.loads(state_json.read_text())
        assert result["targets"]["global"]["claude"]["profile"] == "default"

    def test_unlink_unknown_name_exits(self, tmp_path):
        state_json, _, _ = self._setup(tmp_path)
        state_json.write_text(json.dumps({"linked_profiles": []}))
        with (
            patch.object(install, "STATE_JSON", state_json),
            pytest.raises(SystemExit) as exc,
        ):
            install._link_profile_unlink("nope", run_init=False)
        assert exc.value.code == 1

    # --- _sweep_symlinks_into ---

    def test_sweep_removes_dangling_symlink(self, tmp_path):
        """Q3 regression: dangling symlink whose target was deleted alongside the linked profile."""
        state_json, claude_home, _ = self._setup(tmp_path)
        external = tmp_path / "external" / "brain"
        target = external / ".claude" / "rules" / "brain-only.md"
        target.parent.mkdir(parents=True)
        target.write_text("# brain rule")
        link = claude_home / "rules" / "brain-only.md"
        link.symlink_to(target)
        # Delete the external profile dir entirely → link is now dangling
        import shutil

        shutil.rmtree(external)
        assert link.is_symlink() and not link.exists()  # dangling
        with patch.object(install, "CLAUDE_HOME", claude_home):
            install._sweep_symlinks_into(external)
        assert not link.exists() and not link.is_symlink()

    def test_sweep_preserves_unrelated_symlinks(self, tmp_path):
        state_json, claude_home, _ = self._setup(tmp_path)
        unrelated = tmp_path / "elsewhere" / "rule.md"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("# elsewhere")
        link = claude_home / "rules" / "rule.md"
        link.symlink_to(unrelated)
        # Sweep against an unrelated dir
        with patch.object(install, "CLAUDE_HOME", claude_home):
            install._sweep_symlinks_into(tmp_path / "external" / "brain")
        assert link.exists()  # untouched

    # --- _resolve_profile_chain stale-path resilience ---

    def test_resolve_chain_drops_missing_continues(self, tmp_path, capsys):
        """Q6 regression: stale linked path must not brick the chain — surviving members proceed."""
        state_json, _, profiles_dir = self._setup(tmp_path)
        state_json.write_text(json.dumps({"linked_profiles": [{"name": "brain", "path": "/nonexistent/path"}]}))
        with (
            patch.object(install, "STATE_JSON", state_json),
            patch.object(install, "PROFILES_DIR", profiles_dir),
            patch.object(install, "_get_bundle_path", return_value=None),
        ):
            result = install._resolve_profile_chain("anton,brain")
        assert len(result) == 1
        assert result[0][0] == "anton"
        captured = capsys.readouterr().out
        assert "link-profile unlink brain" in captured  # hint included


# ---------------------------------------------------------------------------
# B1 — init --link-profile NAME=PATH (CLI consolidation)
# ---------------------------------------------------------------------------


class TestInitLinkProfileFlag:
    def _make_args(self, **overrides):
        import argparse

        ns = argparse.Namespace(
            profile=None,
            init_profile=None,
            init_settings_profile=None,
            settings_profile=None,
            bundle=None,
            repo=None,
            local=False,
            force=False,
            dry_run=False,
            query=False,
            list_profiles=False,
            link_profile=[],
            no_discover=True,
        )
        for k, v in overrides.items():
            setattr(ns, k, v)
        return ns

    def test_single_link_profile_invokes_helper(self, tmp_path):
        external = tmp_path / "ext-profile"
        external.mkdir()
        (external / "profile.yml").write_text("name: ext-profile\n")
        args = self._make_args(link_profile=[f"foo={external}"], profile="default")
        with (
            patch.object(install, "_load_state", return_value={"targets": {"global": {"profile": "default"}}}),
            patch.object(install, "_get_bundle_path", return_value=None),
            patch.object(install, "install_global") as mock_install,
            patch.object(install, "_link_profile_link") as mock_link,
        ):
            install.cmd_init_unified(args)
        # Helper was called with the resolved directory and the chosen alias.
        assert mock_link.called
        call = mock_link.call_args
        assert call.args[0] == external.resolve()
        assert call.kwargs["name"] == "foo"
        assert call.kwargs["run_init"] is False
        assert mock_install.called  # global install still runs

    def test_multiple_link_profiles(self, tmp_path):
        ext1 = tmp_path / "p1"
        ext1.mkdir()
        ext2 = tmp_path / "p2"
        ext2.mkdir()
        args = self._make_args(link_profile=[f"a={ext1}", f"b={ext2}"], profile="default")
        with (
            patch.object(install, "_load_state", return_value={"targets": {"global": {"profile": "default"}}}),
            patch.object(install, "_get_bundle_path", return_value=None),
            patch.object(install, "install_global"),
            patch.object(install, "_link_profile_link") as mock_link,
        ):
            install.cmd_init_unified(args)
        assert mock_link.call_count == 2
        names = [c.kwargs["name"] for c in mock_link.call_args_list]
        assert names == ["a", "b"]

    def test_invalid_format_exits(self, tmp_path):
        args = self._make_args(link_profile=["malformed-no-equals"], profile="default")
        with (
            patch.object(install, "_load_state", return_value={}),
            patch.object(install, "_get_bundle_path", return_value=None),
            patch.object(install, "install_global"),
        ):
            with pytest.raises(SystemExit):
                install.cmd_init_unified(args)

    def test_missing_directory_exits(self, tmp_path):
        args = self._make_args(link_profile=[f"foo={tmp_path / 'does-not-exist'}"], profile="default")
        with (
            patch.object(install, "_load_state", return_value={}),
            patch.object(install, "_get_bundle_path", return_value=None),
            patch.object(install, "install_global"),
        ):
            with pytest.raises(SystemExit):
                install.cmd_init_unified(args)


# ---------------------------------------------------------------------------
# B2 — bundle auto-discover hint
# ---------------------------------------------------------------------------


class TestBundleDiscoverHint:
    def test_prints_env_var_candidate_when_valid(self, tmp_path, monkeypatch, capsys):
        bundle = tmp_path / "mybundle"
        (bundle / "profiles").mkdir(parents=True)
        monkeypatch.setenv("AGENTIHOOKS_BUNDLE_PATH", str(bundle))
        install._print_bundle_discover_hint(profile_hint="anton")
        out = capsys.readouterr().out
        assert "AGENTIHOOKS_BUNDLE_PATH" in out
        assert str(bundle.resolve()) in out
        assert "agentihooks init --bundle" in out

    def test_prints_generic_hint_when_no_env(self, monkeypatch, capsys):
        monkeypatch.delenv("AGENTIHOOKS_BUNDLE_PATH", raising=False)
        install._print_bundle_discover_hint(profile_hint="default")
        out = capsys.readouterr().out
        assert "agentihooks init --bundle" in out

    def test_does_not_mutate_state(self, tmp_path, monkeypatch):
        # Read-only: must NOT touch state.json.
        sj = tmp_path / "state.json"
        sj.write_text(json.dumps({"version": "1.0", "targets": {"global": {"profile": "anton"}}}))
        monkeypatch.setattr(install, "STATE_JSON", sj)
        before = sj.read_text()
        install._print_bundle_discover_hint(profile_hint="anton")
        assert sj.read_text() == before


# ---------------------------------------------------------------------------
# CLAUDE.md ownership detection — uninstall must remove real files, not only
# legacy symlinks (regression: install writes a real file for WSL path safety)
# ---------------------------------------------------------------------------


class TestClaudeMdManagedDetection:
    def _wire(self, tmp_path, monkeypatch):
        state = tmp_path / "state.json"
        monkeypatch.setattr(install, "STATE_JSON", state)
        monkeypatch.setattr(install, "AGENTIHOOKS_STATE_DIR", tmp_path)
        return state

    def test_real_file_recorded_in_state_is_managed(self, tmp_path, monkeypatch):
        self._wire(tmp_path, monkeypatch)
        cm = tmp_path / "CLAUDE.md"
        cm.write_text("hand-written, no marker\n")
        install._record_managed_claude_md(cm)
        assert install._claude_md_is_managed(cm) is True

    def test_real_file_with_manifesto_marker_is_managed(self, tmp_path, monkeypatch):
        # state.json predates managed_claude_md → content marker is the fallback
        self._wire(tmp_path, monkeypatch)
        cm = tmp_path / "CLAUDE.md"
        cm.write_text(f"prompt\n<!-- {install._CLAUDE_MD_MANAGED_MARKER} -->\n")
        assert install._claude_md_is_managed(cm) is True

    def test_unmanaged_real_file_is_not_managed(self, tmp_path, monkeypatch):
        self._wire(tmp_path, monkeypatch)
        cm = tmp_path / "CLAUDE.md"
        cm.write_text("the user's own CLAUDE.md, agentihooks never touched it\n")
        assert install._claude_md_is_managed(cm) is False

    def test_missing_file_is_not_managed(self, tmp_path, monkeypatch):
        self._wire(tmp_path, monkeypatch)
        assert install._claude_md_is_managed(tmp_path / "CLAUDE.md") is False

    def test_legacy_symlink_into_profiles_is_managed(self, tmp_path, monkeypatch):
        self._wire(tmp_path, monkeypatch)
        root = tmp_path / "agentihooks"
        monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", root)
        target = root / "profiles" / "anton" / "CLAUDE.md"
        target.parent.mkdir(parents=True)
        target.write_text("profile prompt\n")
        cm = tmp_path / "CLAUDE.md"
        cm.symlink_to(target)
        assert install._claude_md_is_managed(cm) is True

    def test_foreign_symlink_into_profiles_is_not_managed(self, tmp_path, monkeypatch):
        """A profiles/ path in someone else's tree is not proof of ownership."""
        self._wire(tmp_path, monkeypatch)
        monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", tmp_path / "agentihooks")
        target = tmp_path / "dotfiles" / "profiles" / "work" / "CLAUDE.md"
        target.parent.mkdir(parents=True)
        target.write_text("the operator's own prompt\n")
        cm = tmp_path / "CLAUDE.md"
        cm.symlink_to(target)
        assert install._claude_md_is_managed(cm) is False

    def test_record_is_idempotent(self, tmp_path, monkeypatch):
        state = self._wire(tmp_path, monkeypatch)
        cm = tmp_path / "CLAUDE.md"
        cm.write_text("x\n")
        install._record_managed_claude_md(cm)
        first = state.read_text()
        install._record_managed_claude_md(cm)
        assert state.read_text() == first
        assert json.loads(first)["managed_claude_md"] == str(cm)


# ---------------------------------------------------------------------------
# CLAUDE.md is additive (profile body + appended manifesto), never a symlink.
# Uninstall must restore the pre-agentihooks original, or delete if none.
# ---------------------------------------------------------------------------


class TestClaudeMdOriginalBackup:
    def _wire(self, tmp_path, monkeypatch):
        state = tmp_path / "state.json"
        monkeypatch.setattr(install, "STATE_JSON", state)
        monkeypatch.setattr(install, "AGENTIHOOKS_STATE_DIR", tmp_path)
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(install, "CLAUDE_HOME", home)
        return state, home

    def test_first_install_records_user_original(self, tmp_path, monkeypatch):
        state, home = self._wire(tmp_path, monkeypatch)
        dst = home / "CLAUDE.md"
        dst.write_text("the user's own prompt\n")  # pre-agentihooks
        backup = install._backup_existing_claude_md(dst)
        assert backup is not None and backup.exists()
        assert json.loads(state.read_text())["claude_md_original_backup"] == str(backup)

    def test_reinstall_does_not_clobber_original_record(self, tmp_path, monkeypatch):
        state, home = self._wire(tmp_path, monkeypatch)
        dst = home / "CLAUDE.md"
        dst.write_text("user original\n")
        first = install._backup_existing_claude_md(dst)
        install._record_managed_claude_md(dst)  # now agentihooks-managed
        dst.write_text("agentihooks content\n")
        install._backup_existing_claude_md(dst)  # re-install backup
        assert json.loads(state.read_text())["claude_md_original_backup"] == str(first)

    def test_no_backup_when_absent_or_symlink(self, tmp_path, monkeypatch):
        state, home = self._wire(tmp_path, monkeypatch)
        dst = home / "CLAUDE.md"
        assert install._backup_existing_claude_md(dst) is None  # absent
        target = home / "src.md"
        target.write_text("x\n")
        dst.symlink_to(target)
        assert install._backup_existing_claude_md(dst) is None  # symlink

    def test_uninstall_restores_original(self, tmp_path, monkeypatch):
        state, home = self._wire(tmp_path, monkeypatch)
        dst = home / "CLAUDE.md"
        dst.write_text("USER ORIGINAL\n")
        install._backup_existing_claude_md(dst)
        dst.unlink()
        dst.write_text(f"profile body\n<!-- {install._CLAUDE_MD_MANAGED_MARKER} -->\n")
        install._record_managed_claude_md(dst)

        args = install.argparse.Namespace(yes=True)
        with (
            patch.object(install, "_collect_all_managed_mcp_servers", return_value={}),
            patch.object(install, "_get_user_scope_mcp_names", return_value=set()),
            patch.object(install, "_remove_agentihooks_symlinks", return_value=0),
            patch.object(install, "_remove_bashrc_block", return_value=False),
            patch.object(install, "_uninstall_cli_tool"),
        ):
            install.uninstall_global(args)

        assert dst.exists() and dst.read_text() == "USER ORIGINAL\n"
        st = json.loads(state.read_text())
        assert "managed_claude_md" not in st
        assert "claude_md_original_backup" not in st

    def test_uninstall_deletes_when_no_original(self, tmp_path, monkeypatch):
        state, home = self._wire(tmp_path, monkeypatch)
        dst = home / "CLAUDE.md"
        dst.write_text(f"pure agentihooks\n<!-- {install._CLAUDE_MD_MANAGED_MARKER} -->\n")
        install._record_managed_claude_md(dst)

        args = install.argparse.Namespace(yes=True)
        with (
            patch.object(install, "_collect_all_managed_mcp_servers", return_value={}),
            patch.object(install, "_get_user_scope_mcp_names", return_value=set()),
            patch.object(install, "_remove_agentihooks_symlinks", return_value=0),
            patch.object(install, "_remove_bashrc_block", return_value=False),
            patch.object(install, "_uninstall_cli_tool"),
        ):
            install.uninstall_global(args)

        assert not dst.exists()


# ---------------------------------------------------------------------------
# Single-profile installs must carry the `<!-- profile: name -->` marker too
# (regression: the init-loss guard sniffs this marker, but only the chain
# writer emitted it — single profile installs, the common shape, were a
# guard no-op).
# ---------------------------------------------------------------------------


class TestInstallSystemPromptMarker:
    def _wire(self, tmp_path, monkeypatch):
        state = tmp_path / "state.json"
        monkeypatch.setattr(install, "STATE_JSON", state)
        monkeypatch.setattr(install, "AGENTIHOOKS_STATE_DIR", tmp_path)
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(install, "CLAUDE_HOME", home)
        return state, home

    def test_single_profile_install_writes_marker(self, tmp_path, monkeypatch):
        _, home = self._wire(tmp_path, monkeypatch)
        profile_dir = tmp_path / "profiles" / "anton"
        profile_dir.mkdir(parents=True)
        (profile_dir / "CLAUDE.md").write_text("# Anton Profile\nbody\n")

        install._install_system_prompt(profile_dir, "anton")

        dst = home / "CLAUDE.md"
        assert dst.read_text() == "<!-- profile: anton -->\n# Anton Profile\nbody\n"

    def test_source_profile_file_never_mutated(self, tmp_path, monkeypatch):
        self._wire(tmp_path, monkeypatch)
        profile_dir = tmp_path / "profiles" / "anton"
        profile_dir.mkdir(parents=True)
        src = profile_dir / "CLAUDE.md"
        src.write_text("# Anton Profile\nbody\n")

        install._install_system_prompt(profile_dir, "anton")

        assert src.read_text() == "# Anton Profile\nbody\n"

    def test_rerun_is_idempotent_no_marker_stacking(self, tmp_path, monkeypatch, capsys):
        _, home = self._wire(tmp_path, monkeypatch)
        profile_dir = tmp_path / "profiles" / "anton"
        profile_dir.mkdir(parents=True)
        (profile_dir / "CLAUDE.md").write_text("# Anton Profile\nbody\n")

        install._install_system_prompt(profile_dir, "anton")
        first = (home / "CLAUDE.md").read_text()
        install._install_system_prompt(profile_dir, "anton")
        second = (home / "CLAUDE.md").read_text()

        assert first == second
        assert second.count("<!-- profile: anton -->") == 1
        assert "already up to date" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Init-loss guard — a bare init that resolves to 'default' while the
# installed CLAUDE.md carries a different profile marker must abort rather
# than silently overwrite the live profile.
# ---------------------------------------------------------------------------


class TestInitLostStateGuard:
    def _args(self):
        import argparse

        return argparse.Namespace(
            profile=None,
            init_settings_profile=None,
            bundle=None,
            repo=None,
            query=False,
            list_profiles=False,
        )

    def test_single_profile_marker_triggers_abort(self, tmp_path, monkeypatch):
        import os

        cm = tmp_path / "CLAUDE.md"
        cm.write_text("<!-- profile: anton -->\nAnton prompt body\n")
        state = {"managed_claude_md": str(cm), "targets": {}}
        with (
            patch.object(install, "_load_state", return_value=state),
            patch.dict("os.environ", {}, clear=False),
        ):
            os.environ.pop("AGENTIHOOKS_PROFILE", None)
            with pytest.raises(SystemExit):
                install.cmd_init_unified(self._args())

    def test_empty_existing_file_does_not_crash(self, tmp_path, monkeypatch):
        import os

        cm = tmp_path / "CLAUDE.md"
        cm.write_text("")  # existing but empty — used to raise IndexError
        state = {"managed_claude_md": str(cm), "targets": {}}
        with (
            patch.object(install, "_load_state", return_value=state),
            patch.object(install, "_get_bundle_path", return_value=None),
            patch.object(install, "install_global") as mock_install,
            patch.dict("os.environ", {}, clear=False),
        ):
            os.environ.pop("AGENTIHOOKS_PROFILE", None)
            install.cmd_init_unified(self._args())  # must not raise
            called_args = mock_install.call_args[0][0]
            assert called_args.profile == "default"

    def test_missing_file_does_not_crash(self, tmp_path, monkeypatch):
        import os

        cm = tmp_path / "CLAUDE.md"  # never created
        state = {"managed_claude_md": str(cm), "targets": {}}
        with (
            patch.object(install, "_load_state", return_value=state),
            patch.object(install, "_get_bundle_path", return_value=None),
            patch.object(install, "install_global") as mock_install,
            patch.dict("os.environ", {}, clear=False),
        ):
            os.environ.pop("AGENTIHOOKS_PROFILE", None)
            install.cmd_init_unified(self._args())  # must not raise
            called_args = mock_install.call_args[0][0]
            assert called_args.profile == "default"

    def test_chained_marker_suggests_full_chain(self, tmp_path, monkeypatch, capsys):
        import os

        cm = tmp_path / "CLAUDE.md"
        cm.write_text("<!-- profile: coding -->\nCoding body\n\n---\n\n<!-- profile: anton -->\nAnton body\n")
        state = {"managed_claude_md": str(cm), "targets": {}}
        with (
            patch.object(install, "_load_state", return_value=state),
            patch.dict("os.environ", {}, clear=False),
        ):
            os.environ.pop("AGENTIHOOKS_PROFILE", None)
            with pytest.raises(SystemExit):
                install.cmd_init_unified(self._args())
        err = capsys.readouterr().err
        assert "agentihooks init --profile coding,anton" in err


# ---------------------------------------------------------------------------
# _save_state keeps one generation of state.json.bak so a profile-losing
# init is always forensically reconstructable.
# ---------------------------------------------------------------------------


class TestSaveStateBackup:
    def _wire(self, tmp_path, monkeypatch):
        state_json = tmp_path / "state.json"
        monkeypatch.setattr(install, "STATE_JSON", state_json)
        monkeypatch.setattr(install, "AGENTIHOOKS_STATE_DIR", tmp_path)
        return state_json

    def test_creates_bak_with_prior_content(self, tmp_path, monkeypatch):
        state_json = self._wire(tmp_path, monkeypatch)
        install._save_state({"targets": {"global": {"profile": "anton"}}})
        first_content = state_json.read_text()

        install._save_state({"targets": {"global": {"profile": "coding"}}})

        bak = state_json.with_suffix(".json.bak")
        assert bak.exists()
        assert bak.read_text() == first_content
        # _save_state persists verbatim (shape migration happens on load).
        assert json.loads(state_json.read_text())["targets"]["global"]["profile"] == "coding"

    def test_no_bak_on_first_save(self, tmp_path, monkeypatch):
        state_json = self._wire(tmp_path, monkeypatch)
        install._save_state({"targets": {}})
        assert not state_json.with_suffix(".json.bak").exists()


# ---------------------------------------------------------------------------
# Chain edits are per-target (link-profile / settings-profile)
# ---------------------------------------------------------------------------


class TestChainTargets:
    """`link-profile` and `settings-profile` edit every installed target's
    chain unless --for-target narrows it. Writing only claude's record is how
    a codex chain silently diverges."""

    BOTH = {
        "targets": {
            "global": {
                "claude": {"profile": "anton", "path": "/h/.claude"},
                "codex": {"profile": "anton", "path": "/h/.codex"},
            }
        }
    }

    def _setup(self, tmp_path: Path, state: dict):
        state_json = tmp_path / "state.json"
        state_json.write_text(json.dumps(state))
        profiles_dir = tmp_path / "profiles"
        (profiles_dir / "anton").mkdir(parents=True)
        external = tmp_path / "external" / "brain"
        external.mkdir(parents=True)
        return state_json, profiles_dir, external

    # --- _chain_targets resolution ---

    def test_defaults_to_every_installed_target(self, tmp_path):
        state_json, _, _ = self._setup(tmp_path, self.BOTH)
        with patch.object(install, "STATE_JSON", state_json):
            assert install._chain_targets(None) == ("claude", "codex")

    def test_for_target_narrows_to_one(self, tmp_path):
        state_json, _, _ = self._setup(tmp_path, self.BOTH)
        with patch.object(install, "STATE_JSON", state_json):
            assert install._chain_targets("codex") == ("codex",)

    def test_for_target_must_already_be_installed(self, tmp_path, capsys):
        """These commands edit a chain; they do not bootstrap a target.
        Without the guard, --for-target codex on a claude-only machine writes
        a codex record holding only a profile and no path, and every later
        unscoped edit then tries to reinstall a target never set up."""
        state_json, _, _ = self._setup(tmp_path, {"targets": {"global": {"claude": {"profile": "anton"}}}})
        with patch.object(install, "STATE_JSON", state_json), pytest.raises(SystemExit) as exc:
            install._chain_targets("codex")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "not installed" in err and "init --target codex" in err

    def test_link_to_uninstalled_target_writes_no_phantom_record(self, tmp_path):
        state_json, profiles_dir, external = self._setup(
            tmp_path, {"targets": {"global": {"claude": {"profile": "anton"}}}}
        )
        with (
            patch.object(install, "STATE_JSON", state_json),
            patch.object(install, "PROFILES_DIR", profiles_dir),
            patch.object(install, "_get_bundle_path", return_value=None),
            patch.object(install, "install_global") as mock_install,
            pytest.raises(SystemExit),
        ):
            install.cmd_link_profile("link", path=str(external), no_init=True, for_target="codex")
        assert "codex" not in json.loads(state_json.read_text())["targets"]["global"]
        mock_install.assert_not_called()

    def test_settings_profile_error_names_the_empty_scope(self, tmp_path, capsys):
        """An unscoped 'no profile installed' reads as 'nothing is installed'
        even when the other target is."""
        state_json, _, _ = self._setup(
            tmp_path, {"targets": {"global": {"claude": {"profile": "anton"}, "codex": {"path": "/h/.codex"}}}}
        )
        args = MagicMock(sp_name="lean", clear=False, for_target="codex")
        with patch.object(install, "STATE_JSON", state_json), pytest.raises(SystemExit) as exc:
            install._cmd_settings_profile(args)
        assert exc.value.code == 1
        assert "target 'codex'" in capsys.readouterr().err

    def test_list_names_which_targets_carry_each_linked_profile(self, tmp_path, capsys):
        """A name in one target's chain and absent from another's is a
        divergence worth seeing, not something to collapse to '[in chain]'."""
        state = {
            "targets": {"global": {"claude": {"profile": "anton,brain"}, "codex": {"profile": "anton"}}},
            "linked_profiles": [{"name": "brain", "path": str(tmp_path)}],
        }
        state_json, _, _ = self._setup(tmp_path, state)
        with patch.object(install, "STATE_JSON", state_json):
            install._link_profile_list()
        assert "[in chain: claude]" in capsys.readouterr().out

    def test_list_marks_a_profile_in_no_chain(self, tmp_path, capsys):
        state = {
            "targets": {"global": {"claude": {"profile": "anton"}, "codex": {"profile": "anton"}}},
            "linked_profiles": [{"name": "brain", "path": str(tmp_path)}],
        }
        state_json, _, _ = self._setup(tmp_path, state)
        with patch.object(install, "STATE_JSON", state_json):
            install._link_profile_list()
        assert "in chain" not in capsys.readouterr().out

    def test_unknown_for_target_exits(self, tmp_path):
        state_json, _, _ = self._setup(tmp_path, self.BOTH)
        with patch.object(install, "STATE_JSON", state_json), pytest.raises(SystemExit) as exc:
            install._chain_targets("emacs")
        assert exc.value.code == 1

    # --- link ---

    def test_link_appends_to_every_target_chain(self, tmp_path):
        state_json, profiles_dir, external = self._setup(tmp_path, self.BOTH)
        with (
            patch.object(install, "STATE_JSON", state_json),
            patch.object(install, "PROFILES_DIR", profiles_dir),
            patch.object(install, "_get_bundle_path", return_value=None),
        ):
            install._link_profile_link(external, name=None, append=True, run_init=False, targets=("claude", "codex"))
        result = json.loads(state_json.read_text())["targets"]["global"]
        assert result["claude"]["profile"] == "anton,brain"
        assert result["codex"]["profile"] == "anton,brain"

    def test_link_scoped_to_one_target_leaves_the_other_alone(self, tmp_path):
        state_json, profiles_dir, external = self._setup(tmp_path, self.BOTH)
        with (
            patch.object(install, "STATE_JSON", state_json),
            patch.object(install, "PROFILES_DIR", profiles_dir),
            patch.object(install, "_get_bundle_path", return_value=None),
        ):
            install._link_profile_link(external, name=None, append=True, run_init=False, targets=("codex",))
        result = json.loads(state_json.read_text())["targets"]["global"]
        assert result["codex"]["profile"] == "anton,brain"
        assert result["claude"]["profile"] == "anton"

    def test_link_reinstalls_each_target_under_its_own_name(self, tmp_path):
        """Without install_target on the namespace, install_global falls
        through to DEFAULT_TARGET and reinstalls claude twice."""
        state_json, profiles_dir, external = self._setup(tmp_path, self.BOTH)
        with (
            patch.object(install, "STATE_JSON", state_json),
            patch.object(install, "PROFILES_DIR", profiles_dir),
            patch.object(install, "_get_bundle_path", return_value=None),
        ):
            pending = install._link_profile_link(
                external, name=None, append=True, run_init=True, targets=("claude", "codex")
            )
        assert [ns.install_target for ns in pending] == ["claude", "codex"]
        assert {ns.profile for ns in pending} == {"anton,brain"}

    # --- unlink ---

    def test_unlink_strips_every_target_chain(self, tmp_path):
        state = json.loads(json.dumps(self.BOTH))
        state["targets"]["global"]["claude"]["profile"] = "anton,brain"
        state["targets"]["global"]["codex"]["profile"] = "anton,brain"
        state_json, _, external = self._setup(tmp_path, state)
        state = json.loads(state_json.read_text())
        state["linked_profiles"] = [{"name": "brain", "path": str(external)}]
        state_json.write_text(json.dumps(state))
        with (
            patch.object(install, "STATE_JSON", state_json),
            patch.object(install, "CLAUDE_HOME", tmp_path / ".claude"),
            patch.object(install, "_available_profiles", return_value=["anton", "default"]),
        ):
            pending = install._link_profile_unlink("brain", run_init=True, targets=("claude", "codex"))
        result = json.loads(state_json.read_text())["targets"]["global"]
        assert result["claude"]["profile"] == "anton"
        assert result["codex"]["profile"] == "anton"
        assert [ns.install_target for ns in pending] == ["claude", "codex"]

    def test_unlink_ignores_for_target_because_deregistration_is_global(self, tmp_path, capsys):
        """Scoping unlink would leave the other target naming a profile that
        no longer resolves."""
        state = json.loads(json.dumps(self.BOTH))
        state["targets"]["global"]["claude"]["profile"] = "anton,brain"
        state["targets"]["global"]["codex"]["profile"] = "anton,brain"
        state_json, _, external = self._setup(tmp_path, state)
        state = json.loads(state_json.read_text())
        state["linked_profiles"] = [{"name": "brain", "path": str(external)}]
        state_json.write_text(json.dumps(state))
        with (
            patch.object(install, "STATE_JSON", state_json),
            patch.object(install, "CLAUDE_HOME", tmp_path / ".claude"),
            patch.object(install, "_available_profiles", return_value=["anton", "default"]),
            patch.object(install, "install_global") as mock_install,
        ):
            install.cmd_link_profile("unlink", name="brain", no_init=True, for_target="codex")
        result = json.loads(state_json.read_text())["targets"]["global"]
        assert result["claude"]["profile"] == "anton"
        assert result["codex"]["profile"] == "anton"
        assert "--for-target ignored" in capsys.readouterr().out
        mock_install.assert_not_called()

    # --- settings-profile ---

    def test_settings_profile_applies_to_every_target(self, tmp_path):
        state_json, _, _ = self._setup(tmp_path, self.BOTH)
        args = MagicMock(sp_name="lean", clear=False, for_target=None)
        with (
            patch.object(install, "STATE_JSON", state_json),
            patch.object(install, "install_global") as mock_install,
        ):
            install._cmd_settings_profile(args)
        calls = [c.args[0] for c in mock_install.call_args_list]
        assert [ns.install_target for ns in calls] == ["claude", "codex"]
        assert {ns.settings_profile for ns in calls} == {"lean"}

    def test_settings_profile_for_target_scopes_to_one(self, tmp_path):
        state_json, _, _ = self._setup(tmp_path, self.BOTH)
        args = MagicMock(sp_name="lean", clear=False, for_target="codex")
        with (
            patch.object(install, "STATE_JSON", state_json),
            patch.object(install, "install_global") as mock_install,
        ):
            install._cmd_settings_profile(args)
        calls = [c.args[0] for c in mock_install.call_args_list]
        assert [ns.install_target for ns in calls] == ["codex"]

    def test_settings_profile_clear_covers_every_target(self, tmp_path):
        state_json, _, _ = self._setup(tmp_path, self.BOTH)
        args = MagicMock(sp_name=None, clear=True, for_target=None)
        with (
            patch.object(install, "STATE_JSON", state_json),
            patch.object(install, "install_global") as mock_install,
        ):
            install._cmd_settings_profile(args)
        calls = [c.args[0] for c in mock_install.call_args_list]
        assert [ns.install_target for ns in calls] == ["claude", "codex"]
        assert {ns.settings_profile for ns in calls} == {""}

    def test_settings_profile_exits_when_nothing_installed(self, tmp_path):
        state_json, _, _ = self._setup(tmp_path, {})
        args = MagicMock(sp_name="lean", clear=False, for_target=None)
        with patch.object(install, "STATE_JSON", state_json), pytest.raises(SystemExit) as exc:
            install._cmd_settings_profile(args)
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# install_global honours install_target (end-to-end, the one seam nothing ran)
# ---------------------------------------------------------------------------


class TestInstallGlobalHonoursTarget:
    """`install_global` is the function the whole --for-target feature rests on,
    and no test in this suite has ever executed it (see the two
    `inspect.getsource` pins in test_install_validation.py, whose docstrings say
    so). Everything else proves the right Namespace is *built*; nothing proved
    it is *honoured*. It reads `getattr(args, "install_target", "") or
    DEFAULT_TARGET`, so a dropped field silently reinstalls claude — exactly the
    bug this whole line of work fixed.

    Isolation is the autouse conftest fixture: HOME, Path.home, CODEX_HOME and
    both codex resolvers are sandboxed and the fixture refuses to run otherwise.
    """

    def _tiny_profile(self, tmp_path):
        profiles = tmp_path / "profiles"
        tiny = profiles / "tiny" / ".claude"
        tiny.mkdir(parents=True)
        (tiny / "CLAUDE.md").write_text("# tiny persona\n")
        (tiny / "settings.overrides.json").write_text("{}")
        base = profiles / "_base"
        base.mkdir()
        # One native base per target — each target reads its own, in its own format.
        (base / "settings.base.json").write_text(json.dumps({"hooks": {}}))
        (base / "settings.base.copilot.json").write_text(json.dumps({"disableAllHooks": False}))
        (base / "config.base.toml").write_text('approval_policy = "on-request"\n')
        return profiles

    def _run(self, tmp_path, monkeypatch, target):
        profiles = self._tiny_profile(tmp_path)
        monkeypatch.setattr(install, "PROFILES_DIR", profiles, raising=False)
        monkeypatch.setattr(install, "_get_bundle_path", lambda: None)
        # Heavy, network/tool-touching steps — not what this test is about.
        monkeypatch.setattr(install, "_install_cli_tool", lambda *a, **k: None, raising=False)
        monkeypatch.setattr(install, "_ensure_mcp_daemon", lambda *a, **k: None, raising=False)
        # Claude's MCP registration hard-exits unless it can find an interpreter
        # that imports hooks from a neutral cwd — an environment probe, and
        # conftest deliberately points AGENTIHOOKS_ROOT at a scratch dir.
        monkeypatch.setattr(install, "_resolve_hooks_python", lambda *a, **k: Path(sys.executable), raising=False)
        install.install_global(argparse.Namespace(profile="tiny", settings_profile="", install_target=target))
        return Path.home()

    def test_codex_target_writes_codex_and_not_claude(self, tmp_path, monkeypatch):
        home = self._run(tmp_path, monkeypatch, "codex")
        assert (home / ".codex" / "config.toml").exists(), "codex config.toml not written"
        assert (home / ".codex" / "AGENTS.md").exists(), "codex persona not written"
        assert not (home / ".claude" / "settings.json").exists(), (
            "installing codex wrote claude's settings.json — install_target was dropped"
        )

    def test_claude_target_writes_claude_and_not_codex(self, tmp_path, monkeypatch):
        home = self._run(tmp_path, monkeypatch, "claude")
        assert (home / ".claude" / "settings.json").exists(), "claude settings.json not written"
        assert not (home / ".codex" / "config.toml").exists(), "installing claude wrote codex's config.toml"

    def test_target_is_recorded_under_its_own_key(self, tmp_path, monkeypatch):
        self._run(tmp_path, monkeypatch, "codex")
        state = json.loads(install.STATE_JSON.read_text())
        assert state["targets"]["global"]["codex"]["profile"] == "tiny"
        assert "claude" not in state["targets"]["global"]
