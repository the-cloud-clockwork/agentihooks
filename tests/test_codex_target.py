"""Tests for the Codex target adapter (scripts/targets/codex_target.py)."""

import json
import shlex
import subprocess
from pathlib import Path

import install  # binds the installer identity conftest patches; also used directly below
import pytest

from scripts.targets.codex_target import CODEX_HOOK_EVENTS, CodexAdapter, codex_home


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return CodexAdapter()


def _codex_base() -> dict:
    return install._load_native_layer(install.PROFILES_DIR / "_base" / install._NATIVE_BASE_NAME["codex"])


def assert_unambiguous(line: list[str]) -> None:
    context = line.index("context-used")
    assert line[context + 1] == "context-window-size"
    assert not {"context-usage", "used-tokens"} & set(line)


class TestConfigToml:
    @pytest.mark.parametrize("existing", [None, "managed", "custom"])
    def test_init_writes_no_model_catalog(self, adapter, existing):
        from hooks import config

        catalog = config.AGENTIHOOKS_HOME / "codex_model_catalog.json"
        highwater = config.AGENTIHOOKS_HOME / "codex_context_highwater.json"
        config.AGENTIHOOKS_HOME.mkdir(parents=True, exist_ok=True)
        catalog.write_text(json.dumps({"models": [{"slug": "example", "max_context_window": 872000}]}))
        highwater.write_text(json.dumps({"example": 872000}))
        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        config_toml = home / "config.toml"
        value = str(catalog) if existing == "managed" else str(home / "custom.json")
        if existing:
            config_toml.write_text(f'model_catalog_json = "{value}"\n')

        adapter.write_settings({})
        adapter.write_settings({})

        doc = adapter._load_toml(config_toml)
        assert doc.get("model_catalog_json") == (value if existing == "custom" else None)
        assert not catalog.exists()
        assert not highwater.exists()

    def test_list_value_is_managed_and_withdrawn(self, adapter):
        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        adapter.write_settings({"project_doc_fallback_filenames": ["CLAUDE.md"]})
        text = (home / "config.toml").read_text()
        assert 'project_doc_fallback_filenames = ["CLAUDE.md"]' in text
        assert text.count("CLAUDE.md") == 1
        assert json.loads((home / ".agentihooks-managed.json").read_text()) == {
            "project_doc_fallback_filenames": ["CLAUDE.md"]
        }
        adapter.teardown()
        assert "project_doc_fallback_filenames" not in (home / "config.toml").read_text()

    def test_hand_edited_list_is_left_alone(self, adapter):
        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        adapter.write_settings({"project_doc_fallback_filenames": ["CLAUDE.md"]})
        config = home / "config.toml"
        config.write_text(
            config.read_text().replace(
                'project_doc_fallback_filenames = ["CLAUDE.md"]',
                'project_doc_fallback_filenames = ["AGENTS.local.md"]',
                1,
            )
        )
        adapter.write_settings({"project_doc_fallback_filenames": ["CLAUDE.md"]})
        assert 'project_doc_fallback_filenames = ["AGENTS.local.md"]' in config.read_text()

    def test_hand_edits_outside_managed_keys_survive(self, adapter):
        """The highest-value invariant: re-init must never eat operator config."""
        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text(
            '# operator comment\nmodel = "gpt-5.6"\n\n[model_providers.litellm]\nname = "Gateway"\n'
        )
        adapter.write_settings({"features": {"hooks": True}})
        text = (home / "config.toml").read_text()
        assert "# operator comment" in text
        assert 'model = "gpt-5.6"' in text
        assert 'name = "Gateway"' in text
        assert "hooks = true" in text

    def test_native_posture_written(self, adapter):
        """Codex posture is authored natively now, not inferred from a Claude key."""
        adapter.write_settings({"approval_policy": "never", "sandbox_mode": "danger-full-access"})
        text = (codex_home() / "config.toml").read_text()
        assert 'approval_policy = "never"' in text
        assert 'sandbox_mode = "danger-full-access"' in text

    @pytest.mark.parametrize("recorded", [None, "older"])
    def test_matching_posture_is_recorded_without_warning(self, adapter, capsys, recorded):
        import tomlkit

        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        settings = {"approval_policy": "never", "sandbox_mode": "danger-full-access"}
        (home / "config.toml").write_text(tomlkit.dumps(settings))
        if recorded:
            (home / ".agentihooks-managed.json").write_text(
                json.dumps({"approval_policy": "on-request", "sandbox_mode": "workspace-write"})
            )

        adapter.write_settings(settings)

        assert "hand-set" not in capsys.readouterr().out
        assert json.loads((home / ".agentihooks-managed.json").read_text()) == settings
        doc = tomlkit.parse((home / "config.toml").read_text())
        assert {key: doc[key] for key in settings} == settings

    def test_different_unrecorded_posture_warns_and_survives(self, adapter, said):
        import tomlkit

        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        settings = {"approval_policy": "on-request", "sandbox_mode": "workspace-write"}
        (home / "config.toml").write_text(tomlkit.dumps(settings))

        adapter.write_settings({"approval_policy": "never", "sandbox_mode": "danger-full-access"})

        warnings = [line for line in said if "hand-set" in line]
        assert warnings == [
            "  [!!] config.toml 'approval_policy' hand-set to 'on-request' (managed value would be "
            "'never') — leaving operator value in place",
            "  [!!] config.toml 'sandbox_mode' hand-set to 'workspace-write' (managed value would be "
            "'danger-full-access') — leaving operator value in place",
        ]
        doc = tomlkit.parse((home / "config.toml").read_text())
        assert {key: doc[key] for key in settings} == settings
        assert json.loads((home / ".agentihooks-managed.json").read_text()) == {}

    def test_default_translation_does_not_override_operator_choice(self, adapter):
        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text('approval_policy = "untrusted"\n')
        adapter.write_settings({})
        assert 'approval_policy = "untrusted"' in (home / "config.toml").read_text()

    def test_posture_change_restores_managed_values(self, adapter):
        """A profile switching posture must downgrade, not stick forever."""
        adapter.write_settings({"approval_policy": "never", "sandbox_mode": "danger-full-access"})
        text = (codex_home() / "config.toml").read_text()
        assert 'approval_policy = "never"' in text
        adapter.write_settings({"approval_policy": "on-request", "sandbox_mode": "workspace-write"})
        text = (codex_home() / "config.toml").read_text()
        assert 'approval_policy = "on-request"' in text
        assert 'sandbox_mode = "workspace-write"' in text

    def test_operator_hand_set_approval_policy_survives_reinit(self, adapter, capsys):
        adapter.write_settings({"approval_policy": "on-request", "sandbox_mode": "workspace-write"})
        home = codex_home()
        # Operator hand-edits the live key only — not our sidecar record.
        text = (
            (home / "config.toml")
            .read_text()
            .replace('approval_policy = "on-request"', 'approval_policy = "untrusted"', 1)
        )
        (home / "config.toml").write_text(text)
        adapter.write_settings({"approval_policy": "never", "sandbox_mode": "danger-full-access"})
        text = (home / "config.toml").read_text()
        assert 'approval_policy = "untrusted"' in text
        assert "hand-set" in capsys.readouterr().out

    def test_config_carries_no_table_codex_ignores(self, adapter):
        adapter.write_settings({"approval_policy": "never", "tui": {"status_line": ["model"]}})
        home = codex_home()
        assert "agentihooks" not in adapter._load_toml(home / "config.toml")
        record = {"approval_policy": "never", "tui": {"status_line": ["model"]}}
        assert (home / ".agentihooks-managed.json").read_text() == json.dumps(record, indent=2) + "\n"

    def test_unreadable_sidecar_falls_back_to_the_legacy_table(self, adapter, capsys):
        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text(
            'approval_policy = "untrusted"\n\n[agentihooks.managed]\napproval_policy = "on-request"\n'
        )
        (home / ".agentihooks-managed.json").write_text("{not json")
        adapter.write_settings({"approval_policy": "never"})
        assert adapter._load_toml(home / "config.toml")["approval_policy"] == "untrusted"
        assert "hand-set" in capsys.readouterr().out

    def test_legacy_managed_table_moves_to_the_sidecar(self, adapter, capsys):
        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text(
            'approval_policy = "untrusted"\n\n[agentihooks.managed]\napproval_policy = "on-request"\n'
        )
        adapter.write_settings({"approval_policy": "never"})
        doc = adapter._load_toml(home / "config.toml")
        assert "agentihooks" not in doc
        assert doc["approval_policy"] == "untrusted"
        assert "hand-set" in capsys.readouterr().out
        assert json.loads((home / ".agentihooks-managed.json").read_text()) == {"approval_policy": "on-request"}

    def test_status_line_keeps_context_apart_from_cumulative_tokens(self, adapter):
        old = ["model-with-reasoning", "current-dir", "context-usage", "used-tokens", "five-hour-limit"]
        adapter.write_settings({"tui": {"status_line": old}})
        adapter.write_settings(_codex_base())
        assert_unambiguous(adapter._load_toml(codex_home() / "config.toml")["tui"]["status_line"])

    def test_operator_status_line_survives_reinit(self, adapter):
        adapter.write_settings(_codex_base())
        config = codex_home() / "config.toml"
        doc = adapter._load_toml(config)
        doc["tui"]["status_line"] = ["model", "used-tokens"]
        adapter._dump_toml(config, doc)
        adapter.write_settings(_codex_base())
        assert adapter._load_toml(config)["tui"]["status_line"] == ["model", "used-tokens"]


class TestHooksJson:
    def test_context_spilling_is_disabled_on_every_handler(self, adapter):
        """Unset, codex spills any additionalContext over ~2500 tokens to disk and
        shows the model a preview — the project-context injection would never land."""
        adapter.write_settings({})
        data = json.loads((codex_home() / "hooks.json").read_text())
        for event, groups in data["hooks"].items():
            for group in groups:
                for hook in group["hooks"]:
                    if event in ("Stop", "SubagentStop", "SessionEnd", "PreCompact", "PermissionRequest"):
                        assert "additionalContextLimit" not in hook
                    else:
                        assert hook["additionalContextLimit"] == 0

    def test_all_events_wired_to_wrapper(self, adapter):
        adapter.write_settings({})
        doc = json.loads((codex_home() / "hooks.json").read_text())
        assert set(doc["hooks"].keys()) == set(CODEX_HOOK_EVENTS)
        for groups in doc["hooks"].values():
            cmd = groups[-1]["hooks"][0]["command"]
            assert cmd.endswith("agentihooks-hook.sh")
        wrapper = (codex_home() / "agentihooks-hook.sh").read_text()
        assert "AGENTIHOOKS_TARGET=codex" in wrapper

    def test_foreign_hooks_preserved(self, adapter):
        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        foreign = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "/usr/bin/audit-hook"}]}]}}
        (home / "hooks.json").write_text(json.dumps(foreign))
        adapter.write_settings({})
        doc = json.loads((home / "hooks.json").read_text())
        pretool_cmds = [h["command"] for g in doc["hooks"]["PreToolUse"] for h in g["hooks"]]
        assert "/usr/bin/audit-hook" in pretool_cmds
        assert any(c.endswith("agentihooks-hook.sh") for c in pretool_cmds)

    def test_rerun_does_not_stack_own_entries(self, adapter):
        adapter.write_settings({})
        adapter.write_settings({})
        doc = json.loads((codex_home() / "hooks.json").read_text())
        own = [
            h for g in doc["hooks"]["SessionStart"] for h in g["hooks"] if h["command"].endswith("agentihooks-hook.sh")
        ]
        assert len(own) == 1

    def test_rerun_over_unchanged_hooks_prints_no_trust_advice(self, adapter, capsys):
        adapter.write_settings({})
        before = (codex_home() / "hooks.json").stat().st_mtime_ns
        capsys.readouterr()
        adapter.write_settings({})
        adapter.post_install_reconcile([], "")
        out = capsys.readouterr().out
        assert "run /hooks" not in out
        assert "hooks.json unchanged; existing Codex hook trust holds" in out
        assert (codex_home() / "hooks.json").stat().st_mtime_ns == before

    def test_changed_hooks_file_prints_trust_advice_once(self, adapter, capsys):
        adapter.write_settings({})
        path = codex_home() / "hooks.json"
        doc = json.loads(path.read_text())
        del doc["hooks"]["SessionStart"]
        path.write_text(json.dumps(doc))
        capsys.readouterr()
        adapter.write_settings({})
        adapter.post_install_reconcile([], "")
        assert capsys.readouterr().out.count("run /hooks") == 1
        assert "SessionStart" in json.loads(path.read_text())["hooks"]

    @pytest.mark.parametrize("ours_first", [True, False])
    def test_reinstall_keeps_own_group_first_so_codex_trust_positions_hold(self, adapter, ours_first):
        """Codex keys hook trust by `hooks.json:<event>:<group>:<hook>`; moving a group voids its approval."""
        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        ours = {"hooks": [{"type": "command", "command": str(home / "agentihooks-hook.sh")}]}
        herdr = {"hooks": [{"type": "command", "command": "bash herdr-agent-state.sh session", "timeout": 10}]}
        groups = [ours, herdr] if ours_first else [herdr, ours]
        (home / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": groups}}))
        adapter.write_settings({})
        commands = [
            g["hooks"][0]["command"] for g in json.loads((home / "hooks.json").read_text())["hooks"]["SessionStart"]
        ]
        assert commands == [str(home / "agentihooks-hook.sh"), "bash herdr-agent-state.sh session"]

    def test_disabled_foreign_hook_with_wrapper_suffix_preserved(self, adapter):
        """A substring match would misclassify `<wrapper>.disabled-by-operator` as ours."""
        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        wrapper_path = home / "agentihooks-hook.sh"
        disabled_cmd = str(wrapper_path) + ".disabled-by-operator"
        foreign = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": disabled_cmd}]}]}}
        (home / "hooks.json").write_text(json.dumps(foreign))
        adapter.write_settings({})
        doc = json.loads((home / "hooks.json").read_text())
        pretool_cmds = [h["command"] for g in doc["hooks"]["PreToolUse"] for h in g["hooks"]]
        assert disabled_cmd in pretool_cmds
        assert str(wrapper_path) in pretool_cmds

    def test_stale_own_entry_under_unwired_event_reaped(self, adapter):
        """Our wrapper under an event we no longer wire (e.g. PostCompact from an
        earlier install) is reaped; a foreign group under that event survives."""
        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        wrapper_cmd = str(home / "agentihooks-hook.sh")
        stale = {
            "hooks": {
                "PostCompact": [
                    {"hooks": [{"type": "command", "command": wrapper_cmd}]},
                    {"hooks": [{"type": "command", "command": "/usr/local/bin/operator-hook"}]},
                ]
            }
        }
        (home / "hooks.json").write_text(json.dumps(stale))
        adapter.write_settings({})
        doc = json.loads((home / "hooks.json").read_text())
        postcompact_cmds = [h["command"] for g in doc["hooks"].get("PostCompact", []) for h in g["hooks"]]
        assert wrapper_cmd not in postcompact_cmds
        assert "/usr/local/bin/operator-hook" in postcompact_cmds

    def test_stale_own_only_event_removed_entirely(self, adapter):
        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        wrapper_cmd = str(home / "agentihooks-hook.sh")
        stale = {"hooks": {"PostCompact": [{"hooks": [{"type": "command", "command": wrapper_cmd}]}]}}
        (home / "hooks.json").write_text(json.dumps(stale))
        adapter.write_settings({})
        doc = json.loads((home / "hooks.json").read_text())
        assert "PostCompact" not in doc["hooks"]

    def test_wrapper_script_quotes_paths_with_spaces(self, adapter, monkeypatch):
        spaced_root = codex_home().parent / "agenti hooks root"
        monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", spaced_root)
        adapter.write_settings({})
        script = (codex_home() / "agentihooks-hook.sh").read_text()
        assert f"cd {shlex.quote(str(spaced_root))}" in script
        result = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr

    def test_config_toml_and_hooks_json_writes_leave_no_temp_files(self, adapter):
        adapter.write_settings({})
        home = codex_home()
        assert list(home.glob(".config.toml.tmp-*")) == []
        assert list(home.glob(".hooks.json.tmp-*")) == []
        assert (home / "config.toml").exists()
        assert (home / "hooks.json").exists()


class TestSharedSkillsDir:
    def test_codex_reaps_a_copilot_translated_command_shadowing_a_real_skill(self, adapter, tmp_path):
        """~/.agents/skills is shared: codex must clear copilot's translated
        command when a real skill claims that name, or the skill never links."""
        from scripts.targets._common import TRANSLATED_COMMANDS_MANIFEST, agents_skills_home
        from scripts.targets.copilot_target import CopilotAdapter

        cmds = tmp_path / "commands"
        cmds.mkdir()
        (cmds / "shared-name.md").write_text("---\ndescription: cmd\n---\n\nold command\n")
        CopilotAdapter().install_features("commands", [("bundle", cmds)], lambda p: p.suffix == ".md")
        shared = agents_skills_home() / "shared-name"
        assert shared.is_dir() and not shared.is_symlink()
        assert TRANSLATED_COMMANDS_MANIFEST in [f.name for f in agents_skills_home().iterdir()]

        skills_src = tmp_path / "skills"
        (skills_src / "shared-name").mkdir(parents=True)
        (skills_src / "shared-name" / "SKILL.md").write_text("REAL SKILL")
        adapter.install_features("skills", [("bundle", skills_src)], lambda p: p.is_dir())

        assert shared.is_symlink()
        assert (shared / "SKILL.md").read_text() == "REAL SKILL"


class TestPersona:
    def test_agents_md_compiles_chain_rules_and_manifesto(self, adapter, tmp_path, monkeypatch):
        prof = tmp_path / "prof-anton"
        prof.mkdir()
        (prof / "CLAUDE.md").write_text("# Anton persona\n")
        rules_src = tmp_path / "rules"
        rules_src.mkdir()
        (rules_src / "01-style.md").write_text("Always be terse.")
        manifesto = tmp_path / "MANIFESTO.md"
        manifesto.write_text("# Doctrine\n")
        monkeypatch.setenv("CI_MANIFESTO_PATH", str(manifesto))

        adapter.install_features("rules", [("rule", rules_src)], lambda p: p.suffix == ".md")
        adapter.install_persona([("anton", prof)], ["anton"], None)

        text = (codex_home() / "AGENTS.md").read_text()
        assert "<!-- profile: anton -->" in text
        assert "# Anton persona" in text
        # Identity preamble pins the persona ahead of everything else, naming
        # the chain, so codex's own system-prompt identity doesn't win.
        assert "# Identity — who you are" in text
        assert "You are **anton**" in text
        assert text.index("# Identity") < text.index("<!-- profile: anton -->")
        assert "<!-- rule: 01-style.md" in text
        assert "Always be terse." in text
        assert "<!-- ci-manifesto -->" in text
        # Size ceiling landed in config.toml with margin above the payload.
        cfg = (codex_home() / "config.toml").read_text()
        assert "project_doc_max_bytes" in cfg

    def test_unmanaged_agents_md_backed_up(self, adapter, tmp_path):
        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "AGENTS.md").write_text("# operator's own file\n")
        prof = tmp_path / "p"
        prof.mkdir()
        (prof / "CLAUDE.md").write_text("persona")
        adapter.install_persona([("p", prof)], ["p"], None)
        backups = (
            list(home.glob("AGENTS.md.bak.*")) + list(home.glob("AGENTS.md.*.bak*")) + list(home.glob("AGENTS*.bak.*"))
        )
        assert backups, "pre-existing unmanaged AGENTS.md must be backed up"

    def test_operator_content_after_footer_survives_rerun(self, adapter, tmp_path):
        prof = tmp_path / "p"
        prof.mkdir()
        (prof / "CLAUDE.md").write_text("persona v1")
        adapter.install_persona([("p", prof)], ["p"], None)
        dst = codex_home() / "AGENTS.md"
        text = dst.read_text()
        assert "<!-- agentihooks:managed-end -->" in text

        dst.write_text(text + "\n## Operator notes\nDo not touch below this line.\n")

        (prof / "CLAUDE.md").write_text("persona v2")
        adapter.install_persona([("p", prof)], ["p"], None)
        text = dst.read_text()
        assert "persona v2" in text
        assert "persona v1" not in text
        assert "## Operator notes" in text
        assert "Do not touch below this line." in text

    def test_legacy_managed_header_without_footer_backed_up_once(self, adapter, tmp_path):
        from scripts.targets.codex_target import _MANAGED_HEADER

        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "AGENTS.md").write_text(_MANAGED_HEADER + "\nold content, predates the footer marker\n")
        prof = tmp_path / "p"
        prof.mkdir()
        (prof / "CLAUDE.md").write_text("persona")
        adapter.install_persona([("p", prof)], ["p"], None)
        backups = (
            list(home.glob("AGENTS.md.bak.*")) + list(home.glob("AGENTS.md.*.bak*")) + list(home.glob("AGENTS*.bak.*"))
        )
        assert backups, "legacy managed AGENTS.md without a footer marker must be backed up once"


class TestPrompts:
    def _commands(self, tmp_path):
        d = tmp_path / "commands"
        d.mkdir(exist_ok=True)
        (d / "deploy.md").write_text("---\ndescription: Deploy\n---\n\nDeploy now.\n")
        return [("command", d)]

    def test_commands_write_no_prompts_folder(self, adapter, tmp_path):
        adapter.install_features("commands", self._commands(tmp_path), lambda p: p.suffix == ".md")
        assert not (codex_home() / "prompts").exists()

    def test_prompts_an_earlier_install_wrote_are_removed(self, adapter, tmp_path):
        dst_dir = codex_home() / "prompts"
        dst_dir.mkdir(parents=True)
        (dst_dir / "deploy.md").write_text("translated by an earlier install\n")
        (dst_dir / "mine.md").write_text("# operator's own prompt\n")
        (dst_dir / ".agentihooks-manifest.json").write_text('["gone.md", "deploy.md"]')
        adapter.install_features("commands", self._commands(tmp_path), lambda p: p.suffix == ".md")
        assert sorted(p.name for p in dst_dir.iterdir()) == ["mine.md"]


class TestMcp:
    def test_stdio_and_http_translate_sse_skipped(self, adapter, capsys):
        adapter.register_mcp(
            {
                "agentihooks": {"command": "/py", "args": ["-m", "hooks.mcp"]},
                "agentibrain": {"type": "sse", "url": "http://localhost:8104/sse"},
                "remote": {"type": "http", "url": "https://x.example/mcp", "headers": {"A": "B"}},
            }
        )
        text = (codex_home() / "config.toml").read_text()
        assert "[mcp_servers.agentihooks]" in text
        assert "agentibrain" not in text
        assert "[mcp_servers.remote]" in text and "http_headers" in text
        assert "SSE" in capsys.readouterr().out

    def test_bearer_placeholder_maps_to_env_var(self, adapter, capsys):
        adapter.register_mcp(
            {
                "gateway": {
                    "type": "http",
                    "url": "https://g.example/mcp",
                    "headers": {"Authorization": "Bearer ${MCP_GATEWAY_KEY}", "X-Env": "${OTHER}"},
                }
            }
        )
        text = (codex_home() / "config.toml").read_text()
        assert 'bearer_token_env_var = "MCP_GATEWAY_KEY"' in text
        assert "${MCP_GATEWAY_KEY}" not in text, "placeholder must never land literally"
        assert "${OTHER}" not in text
        assert 'X-Env = "OTHER"' in text

    def test_unbraced_bearer_maps_to_env_var(self, adapter):
        adapter.register_mcp(
            {"gw": {"type": "http", "url": "https://gw.example/mcp", "headers": {"Authorization": "Bearer $GW_TOKEN"}}}
        )
        text = (codex_home() / "config.toml").read_text()
        assert 'bearer_token_env_var = "GW_TOKEN"' in text

    def test_whole_header_reference_maps_to_env_http_headers(self, adapter, capsys):
        import tomllib

        headers = {"X-Env": "${OTHER}", "X-Tok": "$MY_TOKEN", "X-Mix": "pre-${MIX}", "X-Def": "${D:-x}"}
        adapter.register_mcp({"gw": {"type": "http", "url": "https://gw.example/mcp", "headers": headers}})
        entry = tomllib.loads((codex_home() / "config.toml").read_text())["mcp_servers"]["gw"]
        assert entry == {"url": "https://gw.example/mcp", "env_http_headers": {"X-Env": "OTHER", "X-Tok": "MY_TOKEN"}}
        printed = " ".join(capsys.readouterr().out.split())
        for header in ("X-Mix", "X-Def"):
            assert f"MCP 'gw' header '{header}' uses a ${{VAR}}/$VAR reference" in printed

    def test_tool_allowlist_and_denylist_survive(self, adapter):
        import tomllib

        tools = ["lf-swarm_traces_by_tag", "lf-swarm_session_timeline"]
        spec = {"type": "http", "url": "https://gw.example/mcp", "enabled_tools": tools, "disabled_tools": ["lf-x"]}
        adapter.register_mcp({"gw": spec})
        entry = tomllib.loads((codex_home() / "config.toml").read_text())["mcp_servers"]["gw"]
        assert entry == {"url": "https://gw.example/mcp", "enabled_tools": tools, "disabled_tools": ["lf-x"]}

    def test_entry_names_why_a_server_cannot_mount(self):
        from scripts.targets.codex_target import codex_mcp_entry

        tok = "ghp_" + "h" * 36
        assert codex_mcp_entry("s", {"type": "sse", "url": "http://x/sse"}) == (None, "codex has no SSE transport")
        assert codex_mcp_entry("n", {"type": "http"}) == (None, "no command or url")
        credentialed = codex_mcp_entry("c", {"type": "http", "url": f"https://u:{tok}@g.example/mcp"})
        assert credentialed == (None, "credential-shaped literal in url, command or args")
        assert codex_mcp_entry("ok", {"command": "/bin/a"}) == ({"command": "/bin/a"}, "")

    def test_credential_in_url_drops_the_whole_server(self, adapter, capsys):
        tok = "ghp_" + "h" * 36
        adapter.register_mcp({"bad": {"type": "http", "url": f"https://user:{tok}@gw.example/mcp"}})
        assert "bad" not in (codex_home() / "config.toml").read_text()
        assert "NOT written" in capsys.readouterr().out

    def test_registered_names_recorded_for_teardown(self, adapter):
        adapter.register_mcp({"a": {"command": "/bin/a"}})
        state = install._load_state()
        assert "a" in install._global_record(state, "codex").get("managed_mcp", [])

    def test_credential_shaped_header_dropped(self, adapter, capsys):
        # Built by concatenation so the literal secret-shaped string never appears
        # whole anywhere else in this file.
        dummy_key = "AKIA" + "TESTDUMMY0000000"
        adapter.register_mcp(
            {
                "gateway": {
                    "type": "http",
                    "url": "https://g.example/mcp",
                    "headers": {"X-Api-Key": dummy_key},
                }
            }
        )
        text = (codex_home() / "config.toml").read_text()
        assert dummy_key not in text
        out = capsys.readouterr().out
        assert "X-Api-Key" in out
        assert "gateway" in out

    def test_credential_concatenated_onto_a_placeholder_is_dropped(self, adapter, capsys):
        """A value merely CONTAINING ${VAR} must not skip the scan."""
        dummy_key = "AKIA" + "TESTDUMMY0000000"
        adapter.register_mcp({"local": {"command": "/py", "env": {"UPSTREAM_KEY": "${SAFE_VAR}-and-" + dummy_key}}})
        text = (codex_home() / "config.toml").read_text()
        assert dummy_key not in text
        assert "UPSTREAM_KEY" in capsys.readouterr().out

    def test_pure_env_reference_still_survives(self, adapter):
        adapter.register_mcp({"local": {"command": "/py", "env": {"UPSTREAM_KEY": "${MY_TOKEN}"}}})
        assert "${MY_TOKEN}" in (codex_home() / "config.toml").read_text()

    def test_env_reference_resolved_by_bash_wrapper(self, adapter):
        import tomllib

        adapter.register_mcp(
            {"pw": {"command": "cmd.exe", "args": ["/c", "npx"], "env": {"TOKEN": "${SRC_TOKEN}", "WSLENV": "TOKEN"}}}
        )
        entry = tomllib.loads((codex_home() / "config.toml").read_text())["mcp_servers"]["pw"]
        assert entry["command"] == "bash"
        assert entry["args"] == ["-c", 'TOKEN="${SRC_TOKEN}" exec "$0" "$@"', "cmd.exe", "/c", "npx"]
        assert entry["env_vars"] == ["SRC_TOKEN"]
        assert entry["env"] == {"WSLENV": "TOKEN"}

    def test_credential_shaped_env_var_dropped(self, adapter, capsys):
        dummy_key = "AKIA" + "TESTDUMMY0000000"
        adapter.register_mcp(
            {
                "local": {
                    "command": "/py",
                    "args": ["-m", "server"],
                    "env": {"UPSTREAM_KEY": dummy_key},
                }
            }
        )
        text = (codex_home() / "config.toml").read_text()
        assert dummy_key not in text
        out = capsys.readouterr().out
        assert "UPSTREAM_KEY" in out
        assert "local" in out

    def test_skills_symlinked_to_agents_dir(self, adapter, tmp_path):
        src = tmp_path / "skills"
        (src / "my-skill").mkdir(parents=True)
        (src / "my-skill" / "SKILL.md").write_text("---\nname: my-skill\n---\nbody")
        adapter.install_features("skills", [("skill", src)], lambda p: p.is_dir())
        from scripts.targets.codex_target import agents_skills_home

        assert (agents_skills_home() / "my-skill").exists()


@pytest.fixture
def said(monkeypatch):
    from scripts.targets._common import _install_module

    lines: list[str] = []
    monkeypatch.setattr(_install_module(), "_cprint", lambda msg, **kwargs: lines.append(msg))
    return lines


class TestCodexEntry:
    def test_stdio_keeps_command_args_and_forwards_references(self, said):
        from scripts.targets.codex_target import codex_mcp_entry

        dummy = "AKIA" + "TESTDUMMY0000000"
        spec = {"command": "/py", "args": ["-m", "s"], "env": {"KEY": dummy, "REF": "${SRC}", "LIT": "1"}}
        assert codex_mcp_entry("loc", spec) == (
            {
                "command": "bash",
                "args": ["-c", 'REF="${SRC}" exec "$0" "$@"', "/py", "-m", "s"],
                "env": {"LIT": "1"},
                "env_vars": ["SRC"],
            },
            "",
        )
        assert said == [
            "  [!!] MCP 'loc' env var 'KEY' looks like a credential (aws_access_key) — dropped from config.toml. "
            "Export it in the shell environment instead of writing it to disk."
        ]

    def test_strict_secrets_are_dropped_and_every_finding_named(self, said):
        from scripts.targets.codex_target import codex_mcp_entry

        both = "AKIA" + "TESTDUMMY0000000 " + "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop"
        assert codex_mcp_entry("loc", {"command": "/py", "env": {"KEY": both}}) == ({"command": "/py"}, "")
        assert codex_mcp_entry("gw", {"url": "https://g.example", "headers": {"K": both}}) == (
            {"url": "https://g.example"},
            "",
        )
        assert said == [
            "  [!!] MCP 'loc' env var 'KEY' looks like a credential (aws_access_key, jwt_token) — dropped from "
            "config.toml. Export it in the shell environment instead of writing it to disk.",
            "  [!!] MCP 'gw' header 'K' looks like a credential (aws_access_key, jwt_token) — dropped from config.toml. "
            "Reference it via Authorization Bearer ${VAR} (mapped to bearer_token_env_var) instead of a literal value.",
        ]

    def test_credential_in_args_names_the_server_and_file(self, said):
        from scripts.targets.codex_target import codex_mcp_entry

        tok = "ghp_" + "h" * 36
        assert codex_mcp_entry("leak", {"command": "/a", "args": ["--t", tok]}) == (
            None,
            "credential-shaped literal in url, command or args",
        )
        assert said == [
            "  [!!] MCP 'leak' carries credential-shaped literals in args[1] (github_token) — server NOT written to "
            "config.toml. Reference secrets via environment variables instead of embedding the value."
        ]

    def test_stdio_without_args_or_env_is_only_its_command(self, said):
        from scripts.targets.codex_target import codex_mcp_entry

        assert codex_mcp_entry("a", {"command": "/a", "args": [], "env": {}}) == ({"command": "/a"}, "")
        assert said == []

    def test_http_headers_split_by_how_codex_can_send_them(self, said):
        from scripts.targets.codex_target import codex_mcp_entry

        dummy = "AKIA" + "TESTDUMMY0000000"
        headers = {
            "authorization": "Bearer ${GW}",
            "Host": "h.example",
            "X-Env": "${E}",
            "X-Bare": "$B",
            "X-Mix": "a-${M}",
            "X-Key": dummy,
        }
        entry, reason = codex_mcp_entry("gw", {"url": "https://g.example/mcp", "headers": headers})
        assert reason == ""
        assert entry == {
            "url": "https://g.example/mcp",
            "bearer_token_env_var": "GW",
            "http_headers": {"Host": "h.example"},
            "env_http_headers": {"X-Env": "E", "X-Bare": "B"},
        }
        assert said == [
            "  [!!] MCP 'gw' header 'X-Mix' uses a ${VAR}/$VAR reference inside a longer value — codex does not "
            "expand these; header dropped. Make the whole value one ${VAR} (mapped to env_http_headers) or an "
            "Authorization Bearer ${VAR} (mapped to bearer_token_env_var).",
            "  [!!] MCP 'gw' header 'X-Key' looks like a credential (aws_access_key) — dropped from config.toml. "
            "Reference it via Authorization Bearer ${VAR} (mapped to bearer_token_env_var) instead of a literal value.",
        ]

    def test_a_non_authorization_bearer_header_is_an_env_header_only_when_whole(self, said):
        from scripts.targets.codex_target import codex_mcp_entry

        entry, _ = codex_mcp_entry("gw", {"url": "https://g.example/mcp", "headers": {"X-Proxy": "Bearer ${P}"}})
        assert entry == {"url": "https://g.example/mcp"}

    def test_filters_copy_and_empty_filters_stay_out(self, said):
        from scripts.targets.codex_target import codex_mcp_entry

        spec = {"url": "https://g.example/mcp", "enabled_tools": ("a", "b"), "disabled_tools": []}
        assert codex_mcp_entry("gw", spec) == ({"url": "https://g.example/mcp", "enabled_tools": ["a", "b"]}, "")

    def test_declared_approval_mode_reaches_codex(self, said):
        from scripts.targets.codex_target import codex_mcp_entry

        spec = {"url": "https://g.example/mcp", "default_tools_approval_mode": "approve"}
        assert codex_mcp_entry("gw", spec) == (
            {"url": "https://g.example/mcp", "default_tools_approval_mode": "approve"},
            "",
        )
        assert codex_mcp_entry("gw", {**spec, "default_tools_approval_mode": ""}) == (
            {"url": "https://g.example/mcp"},
            "",
        )

    def test_unmountable_servers_say_why(self, said):
        from scripts.targets.codex_target import codex_mcp_entry

        assert codex_mcp_entry("old", {"type": "sse", "url": "http://x/sse"}) == (None, "codex has no SSE transport")
        assert codex_mcp_entry("cmd-sse", {"type": "sse", "command": "/a"}) == (None, "codex has no SSE transport")
        assert codex_mcp_entry("typed", {"type": "http", "command": "/a"}) == ({"command": "/a"}, "")
        assert codex_mcp_entry("none", {}) == (None, "no command or url")
        assert said == [
            "  [!!] MCP 'old' uses SSE — codex has no SSE transport; skipped. "
            "Expose a streamable-HTTP endpoint and re-run init.",
            "  [!!] MCP 'cmd-sse' uses SSE — codex has no SSE transport; skipped. "
            "Expose a streamable-HTTP endpoint and re-run init.",
        ]


class TestPersonaIdentityNaming:
    """A linked profile is a capability layer, not part of the persona name."""

    def _profile(self, tmp_path, name):
        d = tmp_path / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "CLAUDE.md").write_text(f"# {name} persona")
        return d

    def test_linked_profile_is_a_layer_not_the_name(self, adapter, tmp_path, monkeypatch):
        monkeypatch.setattr(install, "_load_state", lambda: {"linked_profiles": [{"name": "brain"}]})
        dirs = [("anton", self._profile(tmp_path, "anton")), ("brain", self._profile(tmp_path, "brain"))]
        adapter.install_persona(dirs, ["anton", "brain"], None)
        text = (codex_home() / "AGENTS.md").read_text()
        assert "You are **anton**" in text
        assert "**anton,brain**" not in text
        assert "Layered on top: **brain**" in text

    def test_no_linked_profiles_names_the_base(self, adapter, tmp_path, monkeypatch):
        monkeypatch.setattr(install, "_load_state", lambda: {"linked_profiles": []})
        adapter.install_persona([("anton", self._profile(tmp_path, "anton"))], ["anton"], None)
        text = (codex_home() / "AGENTS.md").read_text()
        assert "You are **anton**" in text
        assert "Layered on top" not in text

    def test_preamble_defers_to_the_precedence_floors(self, adapter, tmp_path, monkeypatch):
        """Two sections each claiming 'read me first' is how a floor gets
        argued away — the identity preamble must yield to Precedence."""
        monkeypatch.setattr(install, "_load_state", lambda: {"linked_profiles": []})
        adapter.install_persona([("anton", self._profile(tmp_path, "anton"))], ["anton"], None)
        text = (codex_home() / "AGENTS.md").read_text()
        assert "It grants no precedence" in text.replace("\n", " ")
        assert "HARD FLOOR) outrank everything here" in text.replace("\n", " ")

    def test_case_mismatch_does_not_leak_a_layer_into_the_name(self, adapter, tmp_path, monkeypatch):
        """linked_profiles stores the alias as typed; a chain written with
        different casing must still treat it as a layer."""
        monkeypatch.setattr(install, "_load_state", lambda: {"linked_profiles": [{"name": "Brain"}]})
        dirs = [("anton", self._profile(tmp_path, "anton")), ("brain", self._profile(tmp_path, "brain"))]
        adapter.install_persona(dirs, ["anton", "brain"], None)
        text = (codex_home() / "AGENTS.md").read_text()
        assert "You are **anton**" in text
        assert "Layered on top: **brain**" in text

    def test_all_linked_chain_recovers_to_the_first_element(self, adapter, tmp_path, monkeypatch):
        """Inconsistent state (every chain entry also registered as linked):
        the chain is written base-first, so chain[0] is the recovery and the
        rest are still described as layers."""
        monkeypatch.setattr(install, "_load_state", lambda: {"linked_profiles": [{"name": "anton"}, {"name": "brain"}]})
        dirs = [("anton", self._profile(tmp_path, "anton")), ("brain", self._profile(tmp_path, "brain"))]
        adapter.install_persona(dirs, ["anton", "brain"], None)
        text = (codex_home() / "AGENTS.md").read_text()
        assert "You are **anton**" in text
        assert "**anton,brain**" not in text

    def test_install_module_accepts_the_main_identity(self, monkeypatch):
        """`python scripts/install.py` registers the installer as __main__."""
        import sys

        from scripts.targets import codex_target

        monkeypatch.delitem(sys.modules, "install", raising=False)
        monkeypatch.delitem(sys.modules, "scripts.install", raising=False)
        fake = type(sys)("__main__")
        fake.__file__ = "/somewhere/scripts/install.py"
        fake.MARKER = "from-main"
        monkeypatch.setitem(sys.modules, "__main__", fake)
        assert codex_target._install_module().MARKER == "from-main"


class TestHooksUtilsTransport:
    """The url form must never hardcode a scheme, and the transport must be
    resolved the same way the claude path resolves it (sonar S5332 flagged the
    hardcoded http:// here after the claude path had already fixed it)."""

    def _entry(self, monkeypatch, **env):
        import install as _i
        from targets.codex_target import CodexAdapter

        for k, v in env.items():
            monkeypatch.setenv(k, v)
        captured: dict = {}
        adapter = CodexAdapter()
        monkeypatch.setattr(adapter, "register_mcp", lambda servers: captured.update(servers))
        monkeypatch.setattr(_i, "_detect_venv", lambda: None)
        adapter.register_hooks_utils("default")
        return captured["agentihooks"]

    def test_stdio_is_a_command_entry(self, monkeypatch):
        entry = self._entry(monkeypatch, AGENTIHOOKS_MCP_TRANSPORT="stdio")
        assert entry["args"] == ["-m", "hooks.mcp"]
        assert "url" not in entry

    def test_url_mode_defaults_to_http_on_loopback(self, monkeypatch):
        entry = self._entry(monkeypatch, AGENTIHOOKS_MCP_TRANSPORT="streamable-http")
        assert entry["url"] == "http://localhost:8642/mcp"

    def test_url_mode_honours_mcp_scheme(self, monkeypatch):
        """An operator fronting the daemon with TLS needs https — the scheme is
        a knob on both targets, not a literal on one of them."""
        entry = self._entry(
            monkeypatch,
            AGENTIHOOKS_MCP_TRANSPORT="streamable-http",
            MCP_SCHEME="https",
            MCP_HOST="mcp.internal",
            MCP_PORT="9443",
        )
        assert entry["url"] == "https://mcp.internal:9443/mcp"


class TestTeardown:
    def _full_install(self, adapter, tmp_path):
        adapter.write_settings({"features": {"hooks": True}})
        adapter.register_mcp({"agentihooks": {"command": "/usr/bin/python", "args": ["-m", "hooks.mcp"]}})
        cmds_src = tmp_path / "commands"
        cmds_src.mkdir(exist_ok=True)
        (cmds_src / "review.md").write_text("---\ndescription: X\n---\n\nbody\n")
        adapter.install_features("commands", [("bundle", cmds_src)], lambda p: p.suffix == ".md")
        prof = tmp_path / "prof"
        prof.mkdir(exist_ok=True)
        (prof / "CLAUDE.md").write_text("persona")
        adapter.install_persona([("p", prof)], ["p"], None)

    def test_removes_own_artifacts(self, adapter, tmp_path):
        self._full_install(adapter, tmp_path)
        adapter.teardown()
        home = codex_home()
        assert not (home / "agentihooks-hook.sh").exists()
        assert not (home / "hooks.json").exists()
        assert not (home / "AGENTS.md").exists()
        assert not (home / "prompts" / "review.md").exists()
        text = (home / "config.toml").read_text()
        assert "agentihooks" not in text
        assert "agentihooks" not in text
        assert "notify" not in text
        assert "project_doc_max_bytes" not in text
        assert not (home / ".agentihooks-managed.json").exists()
        assert install._global_record(install._load_state(), "codex").get("managed_mcp") is None

    def test_legacy_install_without_sidecar_tears_down(self, adapter):
        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text(
            'approval_policy = "never"\n\n[agentihooks.managed]\napproval_policy = "never"\n'
        )
        adapter.teardown()
        assert "approval_policy" not in adapter._load_toml(home / "config.toml")

    def test_unrecorded_approval_policy_stays_with_a_warning(self, adapter, capsys):
        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text('approval_policy = "never"\n')
        adapter.teardown()
        assert adapter._load_toml(home / "config.toml")["approval_policy"] == "never"
        assert (
            "  [!!] no managed-key record — approval_policy/sandbox_mode left as-is; review them "
            '(a torn-down bypass install would have set "never"/"danger-full-access").'
        ) in capsys.readouterr().out

    def test_preserves_operator_content(self, adapter, tmp_path):
        import tomlkit

        self._full_install(adapter, tmp_path)
        home = codex_home()
        doc = tomlkit.parse((home / "config.toml").read_text())
        doc["model"] = "gpt-5.6"
        doc["approval_policy"] = "untrusted"  # hand-edit ON a managed key
        (home / "config.toml").write_text(tomlkit.dumps(doc))
        hooks_doc = json.loads((home / "hooks.json").read_text())
        hooks_doc["hooks"]["PreToolUse"].append({"hooks": [{"type": "command", "command": "/usr/bin/operator-hook"}]})
        (home / "hooks.json").write_text(json.dumps(hooks_doc))
        agents_md = home / "AGENTS.md"
        agents_md.write_text(agents_md.read_text() + "\n## operator tail\n")

        adapter.teardown()

        text = (home / "config.toml").read_text()
        assert 'model = "gpt-5.6"' in text
        assert 'approval_policy = "untrusted"' in text
        assert "sandbox_mode" not in text  # unedited managed key withdrawn
        hooks_doc = json.loads((home / "hooks.json").read_text())
        cmds = [h["command"] for g in hooks_doc["hooks"]["PreToolUse"] for h in g["hooks"]]
        assert cmds == ["/usr/bin/operator-hook"]
        assert "## operator tail" in agents_md.read_text()
        assert "managed-by: agentihooks" not in agents_md.read_text()

    def test_idempotent_on_clean_home(self, adapter):
        adapter.teardown()
        adapter.teardown()


class TestTeardownDestructiveEdges:
    def test_header_without_footer_preserves_whole_file_as_backup(self, adapter):
        """The managed region cannot be separated — deleting would destroy the
        operator tail below the missing marker."""
        from scripts.targets.codex_target import _MANAGED_HEADER

        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "AGENTS.md").write_text(_MANAGED_HEADER + "\nmanaged\n\nMY OWN NOTES\n")
        adapter.teardown()
        assert not (home / "AGENTS.md").exists()
        baks = list(home.glob("AGENTS*.bak*"))
        assert baks and any("MY OWN NOTES" in b.read_text() for b in baks)

    def test_unrecorded_operator_hooks_utils_survives(self, adapter, capsys):
        """A name collision alone must not delete the operator's server."""
        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text('[mcp_servers.agentihooks]\ncommand = "/opt/operator-own-tool/bin/server"\n')
        adapter.teardown()
        assert "operator-own-tool" in (home / "config.toml").read_text()
        assert "review it" in capsys.readouterr().out

    def test_unrecorded_but_content_verified_hooks_utils_removed(self, adapter):
        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text(
            '[mcp_servers.agentihooks]\ncommand = "/usr/bin/python"\nargs = ["-m", "hooks.mcp"]\n'
        )
        adapter.teardown()
        assert "hooks.mcp" not in (home / "config.toml").read_text()

    def test_missing_managed_record_warns_and_keeps_permissive_values(self, adapter, capsys):
        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text('approval_policy = "never"\nsandbox_mode = "danger-full-access"\n')
        adapter.teardown()
        text = (home / "config.toml").read_text()
        assert 'approval_policy = "never"' in text
        out = capsys.readouterr().out
        assert "review them" in out
        assert "[RM] Removed from" not in out

    def test_unparseable_hooks_json_backed_up_not_deleted(self, adapter, capsys):
        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "hooks.json").write_text('{"hooks": {"X": [{"command": "operator_hook_i_care_about"}]}],,,')
        adapter.teardown()
        assert not (home / "hooks.json").exists()
        baks = list(home.glob("hooks*.bak*"))
        assert baks and any("operator_hook_i_care_about" in b.read_text() for b in baks)


def _hooks_file(home, session_start):
    home.mkdir(parents=True, exist_ok=True)
    doc = {"hooks": {"Notes": "kept", "SessionStart": session_start, "Stop": [_own_group(home), _HERDR]}}
    (home / "hooks.json").write_text(json.dumps(doc, indent=2))
    return home / "hooks.json"


def _own_group(home):
    return {"hooks": [{"type": "command", "command": str(home / "agentihooks-hook.sh")}]}


_HERDR = {"hooks": [{"command": "bash herdr-agent-state.sh session", "timeout": 10, "type": "command"}]}


class TestRestoreHookOrder:
    def test_a_drifted_file_gets_the_agentihooks_group_back_first(self, adapter):
        from scripts.targets.codex_target import restore_hook_order

        home = codex_home()
        path = _hooks_file(home, [_HERDR, _own_group(home)])

        assert restore_hook_order(home) == ["SessionStart"]
        assert json.loads(path.read_text())["hooks"] == {
            "Notes": "kept",
            "SessionStart": [_own_group(home), _HERDR],
            "Stop": [_own_group(home), _HERDR],
        }

    def test_every_drifted_event_is_restored(self, adapter):
        from scripts.targets.codex_target import restore_hook_order

        home = codex_home()
        path = _hooks_file(home, [_HERDR, _own_group(home)])
        doc = json.loads(path.read_text())
        doc["hooks"]["Stop"].reverse()
        path.write_text(json.dumps(doc))

        assert restore_hook_order(home) == ["SessionStart", "Stop"]
        assert json.loads(path.read_text())["hooks"]["Stop"] == [_own_group(home), _HERDR]

    @pytest.mark.parametrize(
        ("environ", "expected"),
        [
            ({"CODEX_HOME": " ~/cx , /other", "HOME": "/h"}, "~/cx"),
            ({"HOME": "/h"}, "/h/.codex"),
            ({}, "~/.codex"),
        ],
    )
    def test_the_codex_home_comes_from_the_given_environment(self, environ, expected):
        assert codex_home(environ) == Path(expected).expanduser()

    def test_an_approved_file_is_left_untouched(self, adapter):
        from scripts.targets.codex_target import restore_hook_order

        home = codex_home()
        path = _hooks_file(home, [_own_group(home), _HERDR])
        before = (path.read_text(), path.stat().st_mtime_ns)

        assert restore_hook_order(home) == []
        assert (path.read_text(), path.stat().st_mtime_ns) == before

    def test_a_rendered_home_restores_the_operator_file_through_its_link(self, adapter, tmp_path):
        from scripts.targets.codex_target import restore_hook_order

        operator = codex_home()
        path = _hooks_file(operator, [_HERDR, _own_group(operator)])
        rendered = tmp_path / "profiles" / "engineer" / "codex"
        rendered.mkdir(parents=True)
        (rendered / "hooks.json").symlink_to(path)

        assert restore_hook_order(rendered) == ["SessionStart"]
        assert (rendered / "hooks.json").is_symlink()
        assert json.loads(path.read_text())["hooks"]["SessionStart"] == [_own_group(operator), _HERDR]

    @pytest.mark.parametrize("text", [None, "{not json", '{"hooks": []}'])
    def test_a_missing_or_unreadable_file_is_left_alone(self, adapter, text):
        from scripts.targets.codex_target import restore_hook_order

        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        if text is not None:
            (home / "hooks.json").write_text(text)

        assert restore_hook_order(home) == []
        assert (home / "hooks.json").exists() is (text is not None)

    def test_the_restored_file_matches_what_init_writes(self, adapter):
        from scripts.targets.codex_target import restore_hook_order

        home = codex_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [_HERDR]}}))
        adapter.write_settings({})
        installed = (home / "hooks.json").read_text()
        doc = json.loads(installed)
        doc["hooks"]["SessionStart"].reverse()
        (home / "hooks.json").write_text(json.dumps(doc, indent=2))

        assert restore_hook_order(home) == ["SessionStart"]
        assert (home / "hooks.json").read_text() == installed
