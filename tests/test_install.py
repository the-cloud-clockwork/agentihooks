"""Tests for scripts/install.py — loadenv, mcp-lib, interactive uninstall."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add scripts/ to path so we can import install directly
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import install  # noqa: I001


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
            with patch.dict("os.environ", {"VIRTUAL_ENV": str(tmp_path)}):
                result = install._detect_venv()
        assert result == python

    def test_detects_local_venv(self, tmp_path):
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

    def test_returns_none_when_no_venv(self, tmp_path):
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
        assert result["targets"]["global"]["profile"] == "anton,brain"
        assert result["linked_profiles"][0]["name"] == "brain"
        assert result["linked_profiles"][0]["path"] == str(external)
        # --no-init should bump installed_at
        assert "installed_at" in result["targets"]["global"]

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
        assert result["targets"]["global"]["profile"] == "anton,brain"
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
        assert result["targets"]["global"]["profile"] == "anton"
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
        assert result["targets"]["global"]["profile"] == "default"

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
