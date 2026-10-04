"""Tests for the install-target abstraction (scripts/targets/).

Covers: resolve_target precedence, the legacy→keyed state.json shape
migration, per-target coexistence in state, and the codex not-yet-installable
rejection. The claude-byte-identical regression bar is exercised operationally
(tar-diff in the milestone evidence), not here.
"""

import json

import install
import pytest

from scripts.targets import DEFAULT_TARGET, get_adapter, resolve_target


class TestResolveTarget:
    def test_cli_flag_wins(self, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
        assert resolve_target("codex", "claude", (), interactive_ok=False) == "codex"

    def test_env_beats_stored(self, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
        assert resolve_target(None, "claude", (), interactive_ok=False) == "codex"

    def test_stored_alone_no_longer_wins_noninteractive(self, monkeypatch):
        # `stored` is now only the interactive prompt's default — with no
        # installed-target match and no TTY, it must NOT hijack resolution.
        monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
        assert resolve_target(None, "codex", (), interactive_ok=False) == DEFAULT_TARGET

    def test_single_installed_target_wins_noninteractive(self, monkeypatch):
        monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
        assert resolve_target(None, "", ("codex",), interactive_ok=False) == "codex"

    def test_copilot_only_installed_recalls_copilot(self, monkeypatch):
        monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
        assert resolve_target(None, "", ("copilot",), interactive_ok=False) == "copilot"

    def test_three_installed_targets_warn_names_all_and_default(self, monkeypatch, capsys):
        monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
        assert resolve_target(None, "", ("claude", "codex", "copilot"), interactive_ok=False) == DEFAULT_TARGET
        err = capsys.readouterr().err
        for name in ("claude", "codex", "copilot"):
            assert name in err
        assert "--target" in err

    def test_copilot_via_cli_flag_and_env(self, monkeypatch):
        monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
        assert resolve_target("copilot", "claude", interactive_ok=False) == "copilot"
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "copilot")
        assert resolve_target(None, "claude", interactive_ok=False) == "copilot"

    def test_multiple_installed_targets_warn_and_default_noninteractive(self, monkeypatch, capsys):
        monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
        result = resolve_target(None, "codex", ("claude", "codex"), interactive_ok=False)
        assert result == DEFAULT_TARGET
        err = capsys.readouterr().err
        assert "multiple" in err.lower()
        assert "--target" in err

    def test_default_is_claude(self, monkeypatch):
        monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
        assert resolve_target(None, "", (), interactive_ok=False) == DEFAULT_TARGET

    def test_unknown_target_exits(self, monkeypatch):
        monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
        with pytest.raises(SystemExit):
            resolve_target("emacs", "", (), interactive_ok=False)

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
        assert resolve_target("Claude", "", (), interactive_ok=False) == "claude"


class TestStateShapeMigration:
    def test_legacy_flat_record_rewrapped_under_claude(self):
        state = {
            "targets": {
                "global": {
                    "path": "/home/x/.claude",
                    "profile": "anton",
                    "installed_at": "2026-01-01T00:00:00+00:00",
                }
            }
        }
        assert install._migrate_targets_shape(state) is True
        assert state["targets"]["global"]["claude"]["profile"] == "anton"
        # Idempotent: second pass is a no-op
        assert install._migrate_targets_shape(state) is False

    def test_new_shape_untouched(self):
        state = {"targets": {"global": {"claude": {"profile": "anton", "path": "/x"}}}}
        assert install._migrate_targets_shape(state) is False

    def test_empty_state_untouched(self):
        state: dict = {}
        assert install._migrate_targets_shape(state) is False

    def test_load_state_migrates_on_disk(self):
        legacy = {"targets": {"global": {"path": "/h/.claude", "profile": "smith"}}}
        install.STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
        install.STATE_JSON.write_text(json.dumps(legacy))
        state = install._load_state()
        assert state["targets"]["global"]["claude"]["profile"] == "smith"
        on_disk = json.loads(install.STATE_JSON.read_text())
        assert on_disk["targets"]["global"]["claude"]["profile"] == "smith"

    def test_mixed_shape_preserves_codex_and_nests_claude_fields(self):
        # A version-skewed pre-multi-target binary writes flat claude fields
        # back onto an already-migrated targets.global that also holds a
        # keyed codex record. The codex record must survive.
        state = {
            "targets": {
                "global": {
                    "path": "/home/x/.claude",
                    "profile": "anton",
                    "installed_at": "2026-01-01T00:00:00+00:00",
                    "codex": {"path": "/home/x/.codex", "profile": "smith"},
                }
            }
        }
        assert install._migrate_targets_shape(state) is True
        assert state["targets"]["global"]["codex"]["profile"] == "smith"
        assert state["targets"]["global"]["claude"]["profile"] == "anton"
        assert state["targets"]["global"]["claude"]["path"] == "/home/x/.claude"
        # Idempotent
        assert install._migrate_targets_shape(state) is False


class TestGlobalRecord:
    def test_read_missing_returns_empty(self):
        assert install._global_record({}) == {}

    def test_create_materializes(self):
        state: dict = {}
        rec = install._global_record(state, create=True)
        rec["profile"] = "anton"
        assert state["targets"]["global"]["claude"]["profile"] == "anton"

    def test_copilot_record_isolated_from_siblings(self):
        state = {
            "targets": {
                "global": {
                    "claude": {"profile": "anton"},
                    "codex": {"profile": "smith"},
                    "copilot": {"profile": "agenticore"},
                }
            }
        }
        assert install._global_record(state, "copilot")["profile"] == "agenticore"
        assert install._global_record(state, "claude")["profile"] == "anton"
        from hooks.targets import split_global

        assert set(split_global(state["targets"]["global"])) == {"claude", "codex", "copilot"}

    def test_per_target_isolation(self):
        state: dict = {}
        install._global_record(state, "claude", create=True)["profile"] = "anton"
        install._global_record(state, "codex", create=True)["profile"] = "smith"
        assert install._global_record(state, "claude")["profile"] == "anton"
        assert install._global_record(state, "codex")["profile"] == "smith"


class TestRegisterTargetGlobal:
    def test_register_keys_by_target_and_persists_recall(self):
        install._register_target_global("anton", settings_profile="lean")
        state = install._load_state()
        rec = state["targets"]["global"]["claude"]
        assert rec["profile"] == "anton"
        assert rec["settings_profile"] == "lean"
        assert state["install_target"] == "claude"

    def test_second_target_does_not_clobber_first(self):
        install._register_target_global("anton")
        # Simulate a codex record landing next to it (adapter not needed).
        state = install._load_state()
        state["targets"]["global"]["codex"] = {"path": "/h/.codex", "profile": "smith"}
        install._save_state(state)
        install._register_target_global("anton,brain")
        state = install._load_state()
        assert state["targets"]["global"]["codex"]["profile"] == "smith"
        assert state["targets"]["global"]["claude"]["profile"] == "anton,brain"

    @staticmethod
    def _installed(state: dict) -> tuple:
        """Mirror the installed-target extraction cmd_init_unified performs
        before calling resolve_target."""
        return tuple(t for t, v in state.get("targets", {}).get("global", {}).items() if isinstance(v, dict))

    def test_bare_noninteractive_resolve_after_claude_then_codex_init_stays_claude(self, capsys):
        # This is the HIGH-severity hijack: `init --target codex` used to
        # make every later bare, non-interactive `init` silently reinstall
        # codex via the unscoped `install_target` recall key.
        install._register_target_global("anton")
        install._register_target_global("smith", target="codex")
        state = install._load_state()
        stored = state.get("install_target", "")
        assert stored == "codex"  # last-used, as documented — see next test
        installed = self._installed(state)
        result = resolve_target(None, stored, installed, interactive_ok=False)
        assert result == "claude"
        err = capsys.readouterr().err
        assert "multiple" in err.lower()
        assert "--target" in err

    def test_codex_only_state_bare_noninteractive_resolve_recalls_codex(self):
        install._register_target_global("smith", target="codex")
        state = install._load_state()
        installed = self._installed(state)
        stored = state.get("install_target", "")
        result = resolve_target(None, stored, installed, interactive_ok=False)
        assert result == "codex"

    def test_install_target_is_last_used_but_does_not_drive_noninteractive_resolution(self):
        install._register_target_global("anton")
        install._register_target_global("smith", target="codex")
        state = install._load_state()
        # Recall key is last-writer-wins, as documented at the write site.
        assert state["install_target"] == "codex"
        # But with both records present, resolution is ambiguous and falls
        # to DEFAULT_TARGET rather than trusting `install_target`.
        installed = self._installed(state)
        result = resolve_target(None, state["install_target"], installed, interactive_ok=False)
        assert result == "claude"


class TestGetAdapter:
    def test_claude_adapter_resolves(self):
        adapter = get_adapter("claude")
        assert adapter.name == "claude"
        assert adapter.home() == install.CLAUDE_HOME

    def test_codex_adapter_resolves(self):
        adapter = get_adapter("codex")
        assert adapter.name == "codex"
        assert adapter.home().name == ".codex"

    def test_copilot_adapter_resolves(self):
        adapter = get_adapter("copilot")
        assert adapter.name == "copilot"
        assert adapter.home().name == ".copilot"

    def test_every_supported_target_has_an_adapter(self):
        """A name in SUPPORTED_TARGETS with no adapter fails only at install time."""
        from scripts.targets import SUPPORTED_TARGETS

        for target in SUPPORTED_TARGETS:
            assert get_adapter(target).name == target


class TestSharedSanitizer:
    def test_dollar_containing_literal_credential_is_not_split(self):
        """Stripping unbraced $VAR ate the tail of `pa$sword`-style literals,
        pushing them below the patterns' minimum lengths — a scan bypass."""
        from scripts.targets._common import mcp_spec_credential_hits, scannable

        url = "postgres://user:pa$ssword123@db.internal:5432/app"
        assert scannable(url) == url
        assert mcp_spec_credential_hits("s", {"command": "npx", "args": ["-y", url]})

    def test_resolve_env_references(self, monkeypatch):
        from scripts.targets._common import resolve_env_references

        monkeypatch.setenv("TOK", "sekret")
        monkeypatch.delenv("MISSING", raising=False)
        assert resolve_env_references("Bearer ${TOK}") == ("Bearer sekret", True)
        assert resolve_env_references("$TOK") == ("sekret", True)
        assert resolve_env_references("${MISSING}") == ("${MISSING}", False)
        assert resolve_env_references("${MISSING:-fallback}") == ("fallback", True)
        assert resolve_env_references("no refs here") == ("no refs here", True)

    def test_braced_reference_still_stripped(self):
        from scripts.targets._common import mcp_spec_credential_hits

        assert mcp_spec_credential_hits("s", {"url": "postgres://user:${DB_PASS}@host/db"}) == []

    def test_reference_default_text_is_scanned_as_literal(self):
        """`${T:-<token>}` must not hide the token; a fallback IS a literal."""
        from hooks.secrets import scan
        from scripts.targets._common import scannable

        assert scan(scannable("x=${T:-ghp_" + "a" * 36 + "}"), mode="strict")
        assert scan(scannable("x=${T}"), mode="strict") == []

    def test_claude_merge_sanitizes_env_and_headers(self, capsys):
        import json

        install._merge_mcp_to_user_scope(
            {
                "srv": {
                    "command": "npx",
                    "env": {"GH_TOKEN": "ghp_" + "b" * 36, "SAFE": "${REF}", "PLAIN": "hello"},
                    "headers": {"Authorization": "Bearer ghp_" + "c" * 36, "X-Env": "prod"},
                }
            }
        )
        doc = json.loads(install._CLAUDE_JSON.read_text())
        spec = doc["mcpServers"]["srv"]
        assert sorted(spec["env"]) == ["PLAIN", "SAFE"]
        assert list(spec["headers"]) == ["X-Env"]
        assert "credential" in capsys.readouterr().out


class TestSharedSkillsSweep:
    def test_ledger_only_sweep_spares_operator_link_into_our_sources(self, tmp_path):
        """~/.agents/skills is a shared cross-tool dir: destination alone must
        not claim an operator's hand-made symlink into our source tree."""
        from pathlib import Path

        from scripts.targets._common import agents_skills_home

        skills = agents_skills_home()
        skills.mkdir(parents=True, exist_ok=True)
        real = tmp_path / "real-skill"
        real.mkdir()
        ledgered = skills / "ledgered"
        ledgered.symlink_to(real)
        install._state_record_link(ledgered, real, "skill")
        operator = skills / "operator-link"
        operator.symlink_to(Path(install.AGENTIHOOKS_ROOT))

        n = install._remove_agentihooks_symlinks(skills, "skill", ledger_only=True)
        assert n == 1
        assert not ledgered.is_symlink()
        assert operator.is_symlink()


class TestClaudeSettingsEnvScan:
    def test_credential_shaped_env_value_dropped(self, capsys):
        adapter = get_adapter("claude")
        tok = "ghp_" + "k" * 36
        adapter.write_settings({"env": {"LEAKED": tok, "SAFE_REF": "${MY_TOKEN}", "PLAIN": "hello"}})
        import json

        doc = json.loads((install.CLAUDE_HOME / "settings.json").read_text())
        assert "LEAKED" not in doc["env"]
        assert doc["env"]["SAFE_REF"] == "${MY_TOKEN}"
        assert doc["env"]["PLAIN"] == "hello"
        assert "credential" in capsys.readouterr().out

    def test_env_block_all_credentials_removed_entirely(self):
        adapter = get_adapter("claude")
        adapter.write_settings({"env": {"LEAKED": "ghp_" + "m" * 36}})
        import json

        doc = json.loads((install.CLAUDE_HOME / "settings.json").read_text())
        assert "env" not in doc


class TestInstalledTargets:
    def test_lists_every_record_in_supported_order(self):
        state = {"targets": {"global": {"codex": {"profile": "a"}, "claude": {"profile": "b"}}}}
        assert install._installed_targets(state) == ("claude", "codex")

    def test_three_installed_targets_listed_in_supported_order(self):
        state = {
            "targets": {
                "global": {
                    "copilot": {"profile": "c"},
                    "codex": {"profile": "a"},
                    "claude": {"profile": "b"},
                }
            }
        }
        assert install._installed_targets(state) == ("claude", "codex", "copilot")

    def test_empty_state_defaults_to_claude(self):
        assert install._installed_targets({}) == ("claude",)

    def test_default_is_overridable_for_init(self):
        """init must tell 'nothing installed' from 'claude installed' — the
        first prompts, the second recalls."""
        assert install._installed_targets({}, default=()) == ()

    def test_unknown_target_name_is_kept(self):
        state = {"targets": {"global": {"claude": {"profile": "a"}, "future": {"profile": "z"}}}}
        assert install._installed_targets(state) == ("claude", "future")

    def test_legacy_flat_state_reports_claude(self):
        assert install._installed_targets({"targets": {"global": {"profile": "anton"}}}) == ("claude",)


class TestTargetConstantParity:
    """``hooks/targets`` and ``scripts/targets`` each define DEFAULT_TARGET.

    They cannot share one: the hook runtime must not import the installer
    (``scripts`` is not on the hook process's import path), and the installer
    already imports ``hooks``. A tripwire is cheaper than a refactor and turns
    silent drift — which would send hook-runtime reads to a different record
    than the installer writes — into a loud failure.
    """

    def test_default_target_agrees_across_packages(self):
        from hooks.targets import DEFAULT_TARGET as hooks_default
        from scripts.targets import DEFAULT_TARGET as scripts_default

        assert hooks_default == scripts_default

    def test_default_target_is_a_supported_target(self):
        from hooks.targets import DEFAULT_TARGET as hooks_default
        from scripts.targets import SUPPORTED_TARGETS

        assert hooks_default in SUPPORTED_TARGETS


class TestNativeLayerDiscovery:
    """Each target resolves its OWN settings/MCP files. The previous design
    translated one Claude document per target, which could carry only
    permissions.defaultMode and left every target-native capability
    unreachable from a bundle."""

    def _layout(self, tmp_path):
        for sub, name, body in (
            (".claude", "settings.overrides.json", '{"claudeKey": 1}'),
            (".codex", "config.overrides.toml", 'approval_policy = "never"\n'),
            (".copilot", "settings.overrides.json", '{"effortLevel": "high"}'),
        ):
            d = tmp_path / sub
            d.mkdir(parents=True, exist_ok=True)
            (d / name).write_text(body)
        return tmp_path

    def test_each_target_resolves_its_own_file(self, tmp_path):
        root = self._layout(tmp_path)
        for target, expect in (
            ("claude", "settings.overrides.json"),
            ("codex", "config.overrides.toml"),
            ("copilot", "settings.overrides.json"),
        ):
            got = install._native_layer_path(root, target, install._NATIVE_SETTINGS_NAME)
            assert got is not None, target
            assert got.name == expect
            assert got.parent.name == install._TARGET_SUBDIR[target]

    def test_missing_native_file_is_none_not_an_error(self, tmp_path):
        assert install._native_layer_path(tmp_path, "codex", install._NATIVE_SETTINGS_NAME) is None

    def test_layer_root_fallback_for_pre_subdir_layout(self, tmp_path):
        (tmp_path / "settings.overrides.json").write_text('{"legacy": true}')
        got = install._native_layer_path(tmp_path, "claude", install._NATIVE_SETTINGS_NAME)
        assert got is not None and got.parent == tmp_path

    def test_toml_layer_loads_as_plain_dict(self, tmp_path):
        f = tmp_path / "config.overrides.toml"
        f.write_text('approval_policy = "never"\n\n[tui]\nstatus_line = ["a"]\n')
        data = install._load_native_layer(f)
        assert data["approval_policy"] == "never"
        assert data["tui"]["status_line"] == ["a"]

    def test_json_layer_loads(self, tmp_path):
        f = tmp_path / "settings.overrides.json"
        f.write_text('{"effortLevel": "high"}')
        assert install._load_native_layer(f)["effortLevel"] == "high"


class TestClaudeSettingsForeignHooks:
    HERDR = {"hooks": [{"type": "command", "command": "sh '/opt/herdr/herdr-agent-state.sh' session"}]}

    @staticmethod
    def _ours(command: str) -> dict:
        return {"hooks": [{"type": "command", "command": command}]}

    @staticmethod
    def _settings() -> dict:
        import json

        return json.loads((install.CLAUDE_HOME / "settings.json").read_text())

    def _seed(self, hooks: dict) -> None:
        import json

        install.CLAUDE_HOME.mkdir(parents=True, exist_ok=True)
        doc = {install.MANAGED_BY_KEY: install.MANAGED_BY_VALUE, "hooks": hooks}
        (install.CLAUDE_HOME / "settings.json").write_text(json.dumps(doc))

    def test_a_hook_another_tool_wrote_survives_the_write(self):
        self._seed({"SessionStart": [self.HERDR]})
        rendered = {"SessionStart": [self._ours("python -m hooks")], "Stop": [self._ours("python -m hooks")]}
        get_adapter("claude").write_settings({"hooks": rendered})
        hooks = self._settings()["hooks"]
        assert self.HERDR in hooks["SessionStart"]
        assert self._ours("python -m hooks") in hooks["SessionStart"]
        assert hooks["Stop"] == [self._ours("python -m hooks")]

    def test_a_hook_agentihooks_wrote_before_is_replaced_not_kept(self):
        adapter = get_adapter("claude")
        adapter.write_settings({"hooks": {"SessionStart": [self._ours("echo old-profile")]}})
        adapter.write_settings({"hooks": {"SessionStart": [self._ours("echo new-profile")]}})
        assert self._settings()["hooks"] == {"SessionStart": [self._ours("echo new-profile")]}

    def test_a_hook_running_from_an_agentihooks_root_is_ours(self):
        stale = self._ours(f"cd {install.AGENTIHOOKS_ROOT} && python -m hooks.old")
        self._seed({"PreCompact": [stale], "SessionStart": [self.HERDR]})
        get_adapter("claude").write_settings({"hooks": {"SessionStart": [self._ours("python -m hooks")]}})
        hooks = self._settings()["hooks"]
        assert "PreCompact" not in hooks
        assert hooks["SessionStart"] == [self._ours("python -m hooks"), self.HERDR]

    def test_writing_twice_gives_the_same_hooks_as_once(self):
        self._seed({"SessionStart": [self.HERDR]})
        adapter = get_adapter("claude")
        rendered = {"hooks": {"SessionStart": [self._ours("python -m hooks")]}}
        adapter.write_settings(rendered)
        once = self._settings()["hooks"]
        adapter.write_settings(rendered)
        assert self._settings()["hooks"] == once
