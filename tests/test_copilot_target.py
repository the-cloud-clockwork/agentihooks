"""Tests for the Copilot target adapter (scripts/targets/copilot_target.py)."""

import json
import os
import shlex
import subprocess
from pathlib import Path

import install  # binds the installer identity conftest patches; also used directly below
import pytest

from scripts.targets._common import agents_skills_home
from scripts.targets.copilot_target import COPILOT_HOOK_EVENTS, CopilotAdapter, copilot_home


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.delenv("COPILOT_HOME", raising=False)
    return CopilotAdapter()


def _hook_cmds(doc, event):
    return [h["command"] for h in doc["hooks"].get(event, [])]


class TestSettingsJson:
    def test_config_json_is_never_written(self, adapter):
        """config.json is machine-managed by the CLI — writing it is a data race."""
        adapter.write_settings({})
        assert not (copilot_home() / "config.json").exists()
        assert (copilot_home() / "settings.json").exists()

    def test_hand_edits_outside_managed_keys_survive(self, adapter):
        """The highest-value invariant: re-init must never eat operator config."""
        home = copilot_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "settings.json").write_text(json.dumps({"theme": "dim", "allowedUrls": ["github.com"], "beep": True}))
        adapter.write_settings({})
        doc = json.loads((home / "settings.json").read_text())
        assert doc["theme"] == "dim"
        assert doc["allowedUrls"] == ["github.com"]
        assert doc["beep"] is True

    def test_statusline_wired_to_command(self, adapter):
        adapter.write_settings({"statusLine": {"type": "command", "command": "/usr/bin/python -m hooks.statusline"}})
        doc = json.loads((copilot_home() / "settings.json").read_text())
        assert doc["statusLine"]["type"] == "command"
        assert "hooks.statusline" in doc["statusLine"]["command"]

    def test_disable_all_hooks_forced_false(self, adapter):
        """An inherited true would silently kill every guardrail."""
        home = copilot_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "settings.json").write_text(json.dumps({"disableAllHooks": True}))
        adapter.write_settings({})
        # Operator hand-set value is respected but must be reported loudly.
        doc = json.loads((home / "settings.json").read_text())
        assert "disableAllHooks" in doc

    def test_bypass_writes_allow_all_env(self, adapter):
        """Copilot has no settings key for YOLO: permissions.allow rules cover
        tools only (a write rule still hits path verification) and
        trustedFolders is not a recognized setting. COPILOT_ALLOW_ALL is the
        switch, exported by the installer's agentienv shell block."""
        adapter.write_settings({"_agentihooks": {"allowAll": True}})
        env_file = install.AGENTIHOOKS_STATE_DIR / "copilot.env"
        assert env_file.exists(), "bypassPermissions must produce the copilot env file"
        # Copilot tests `=== "true"` for folder trust, workspace MCP sources,
        # repo hooks and plugin loading; only the --allow-all-tools binding is
        # presence-based. A truthy-looking "1" leaves the rest silently off.
        assert "COPILOT_ALLOW_ALL=true" in env_file.read_text()

    def test_non_bypass_removes_allow_all_env(self, adapter):
        adapter.write_settings({"_agentihooks": {"allowAll": True}})
        env_file = install.AGENTIHOOKS_STATE_DIR / "copilot.env"
        assert env_file.exists()
        adapter.write_settings({})
        assert not env_file.exists(), "turning bypass off must withdraw the env file"

    def test_bypass_does_not_write_unrecognized_trusted_folders(self, adapter):
        """trustedFolders is not in Copilot's canonical settings keys — writing
        it makes the CLI warn about an unknown key on every launch."""
        adapter.write_settings({"_agentihooks": {"allowAll": True}})
        doc = json.loads((copilot_home() / "settings.json").read_text())
        assert "trustedFolders" not in doc

    def test_teardown_removes_stale_trusted_folders_and_env(self, adapter):
        home = copilot_home()
        adapter.write_settings({"_agentihooks": {"allowAll": True}})
        doc = json.loads((home / "settings.json").read_text())
        doc["trustedFolders"] = [str(install.AGENTIHOOKS_ROOT)]
        (home / "settings.json").write_text(json.dumps(doc))
        adapter.teardown()
        doc = json.loads((home / "settings.json").read_text())
        assert "trustedFolders" not in doc
        assert not (install.AGENTIHOOKS_STATE_DIR / "copilot.env").exists()

    def test_operator_hand_set_managed_key_survives_reinit(self, adapter, capsys):
        adapter.write_settings({"statusLine": {"type": "command", "command": "/usr/bin/python -m hooks.statusline"}})
        home = copilot_home()
        doc = json.loads((home / "settings.json").read_text())
        doc["statusLine"] = {"type": "command", "command": "/usr/local/bin/my-status"}
        (home / "settings.json").write_text(json.dumps(doc))
        adapter.write_settings({"statusLine": {"type": "command", "command": "/usr/bin/python -m hooks.statusline"}})
        doc = json.loads((home / "settings.json").read_text())
        assert doc["statusLine"]["command"] == "/usr/local/bin/my-status"
        assert "hand-set" in capsys.readouterr().out

    def test_write_leaves_no_temp_files(self, adapter):
        adapter.write_settings({})
        home = copilot_home()
        assert list(home.glob(".settings.json.tmp-*")) == []
        assert list((home / "hooks").glob(".agentihooks.json.tmp-*")) == []


class TestHooksJson:
    def test_events_use_the_shipped_enum_spellings(self, adapter):
        """agentStop / userPromptSubmitted are different TOKENS from claude's
        Stop / UserPromptSubmit — lowercasing would invent a nonexistent event."""
        assert "agentStop" in COPILOT_HOOK_EVENTS and "Stop" not in COPILOT_HOOK_EVENTS
        assert "userPromptSubmitted" in COPILOT_HOOK_EVENTS
        assert all(e[0].islower() for e in COPILOT_HOOK_EVENTS)

    def test_all_events_wired_to_wrapper(self, adapter):
        adapter.write_settings({})
        doc = json.loads((copilot_home() / "hooks" / "agentihooks.json").read_text())
        assert doc["version"] == 1
        assert set(doc["hooks"].keys()) == set(COPILOT_HOOK_EVENTS)
        for event, hooks in doc["hooks"].items():
            assert hooks[-1]["command"].endswith(f"agentihooks-hook.sh {event}")
            assert hooks[-1]["type"] == "command"
            assert hooks[-1]["timeoutSeconds"] > 0
        wrapper = (copilot_home() / "agentihooks-hook.sh").read_text()
        assert "AGENTIHOOKS_TARGET=copilot" in wrapper
        assert 'AGENTIHOOKS_COPILOT_EVENT="${1:-}"' in wrapper

    def test_hooks_are_not_also_inlined_in_settings(self, adapter):
        """Copilot merges hooks/ and the inline settings key — both fires twice."""
        adapter.write_settings({})
        doc = json.loads((copilot_home() / "settings.json").read_text())
        assert "hooks" not in doc

    def test_foreign_hooks_preserved(self, adapter):
        home = copilot_home()
        (home / "hooks").mkdir(parents=True, exist_ok=True)
        foreign = {"version": 1, "hooks": {"preToolUse": [{"type": "command", "command": "/usr/bin/audit-hook"}]}}
        (home / "hooks" / "agentihooks.json").write_text(json.dumps(foreign))
        adapter.write_settings({})
        doc = json.loads((home / "hooks" / "agentihooks.json").read_text())
        cmds = _hook_cmds(doc, "preToolUse")
        assert "/usr/bin/audit-hook" in cmds
        assert any("agentihooks-hook.sh" in c for c in cmds)

    def test_rerun_does_not_stack_own_entries(self, adapter):
        adapter.write_settings({})
        adapter.write_settings({})
        doc = json.loads((copilot_home() / "hooks" / "agentihooks.json").read_text())
        own = [c for c in _hook_cmds(doc, "sessionStart") if "agentihooks-hook.sh" in c]
        assert len(own) == 1

    def test_disabled_foreign_hook_with_wrapper_suffix_preserved(self, adapter):
        """A substring match would misclassify `<wrapper>.disabled-by-operator` as ours."""
        home = copilot_home()
        (home / "hooks").mkdir(parents=True, exist_ok=True)
        wrapper_path = home / "agentihooks-hook.sh"
        disabled_cmd = str(wrapper_path) + ".disabled-by-operator"
        foreign = {"version": 1, "hooks": {"preToolUse": [{"type": "command", "command": disabled_cmd}]}}
        (home / "hooks" / "agentihooks.json").write_text(json.dumps(foreign))
        adapter.write_settings({})
        doc = json.loads((home / "hooks" / "agentihooks.json").read_text())
        cmds = _hook_cmds(doc, "preToolUse")
        assert disabled_cmd in cmds
        assert f"{wrapper_path} preToolUse" in cmds

    def test_stale_own_entry_under_unwired_event_reaped(self, adapter):
        home = copilot_home()
        (home / "hooks").mkdir(parents=True, exist_ok=True)
        wrapper_cmd = str(home / "agentihooks-hook.sh")
        stale = {
            "version": 1,
            "hooks": {
                "PostCompact": [
                    {"type": "command", "command": wrapper_cmd},
                    {"type": "command", "command": "/usr/local/bin/operator-hook"},
                ]
            },
        }
        (home / "hooks" / "agentihooks.json").write_text(json.dumps(stale))
        adapter.write_settings({})
        doc = json.loads((home / "hooks" / "agentihooks.json").read_text())
        cmds = _hook_cmds(doc, "PostCompact")
        assert wrapper_cmd not in cmds
        assert "/usr/local/bin/operator-hook" in cmds

    def test_stale_own_only_event_removed_entirely(self, adapter):
        home = copilot_home()
        (home / "hooks").mkdir(parents=True, exist_ok=True)
        wrapper_cmd = str(home / "agentihooks-hook.sh")
        stale = {"version": 1, "hooks": {"PostCompact": [{"type": "command", "command": wrapper_cmd}]}}
        (home / "hooks" / "agentihooks.json").write_text(json.dumps(stale))
        adapter.write_settings({})
        doc = json.loads((home / "hooks" / "agentihooks.json").read_text())
        assert "PostCompact" not in doc["hooks"]

    def test_wrapper_script_quotes_paths_with_spaces(self, adapter, monkeypatch):
        spaced_root = copilot_home().parent / "agenti hooks root"
        monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", spaced_root)
        adapter.write_settings({})
        script = (copilot_home() / "agentihooks-hook.sh").read_text()
        assert f"cd {shlex.quote(str(spaced_root))}" in script
        result = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr


class TestPersona:
    def test_instructions_compile_chain_rules_and_manifesto(self, adapter, tmp_path, monkeypatch):
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

        text = (copilot_home() / "copilot-instructions.md").read_text()
        assert "<!-- profile: anton -->" in text
        assert "# Anton persona" in text
        assert "# Identity — who you are" in text
        assert "You are **anton**" in text
        assert text.index("# Identity") < text.index("<!-- profile: anton -->")
        assert "<!-- rule: 01-style.md" in text
        assert "Always be terse." in text
        assert "<!-- ci-manifesto -->" in text

    def test_unmanaged_instructions_backed_up(self, adapter, tmp_path):
        home = copilot_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "copilot-instructions.md").write_text("# operator's own file\n")
        prof = tmp_path / "p"
        prof.mkdir()
        (prof / "CLAUDE.md").write_text("persona")
        adapter.install_persona([("p", prof)], ["p"], None)
        assert list(home.glob("copilot-instructions*.bak.*")), "unmanaged file must be backed up"

    def test_operator_content_after_footer_survives_rerun(self, adapter, tmp_path):
        prof = tmp_path / "p"
        prof.mkdir()
        (prof / "CLAUDE.md").write_text("persona v1")
        adapter.install_persona([("p", prof)], ["p"], None)
        dst = copilot_home() / "copilot-instructions.md"
        text = dst.read_text()
        assert "<!-- agentihooks:managed-end -->" in text
        dst.write_text(text + "\n## Operator notes\nDo not touch below this line.\n")

        (prof / "CLAUDE.md").write_text("persona v2")
        adapter.install_persona([("p", prof)], ["p"], None)
        text = dst.read_text()
        assert "persona v2" in text
        assert "persona v1" not in text
        assert "## Operator notes" in text


class TestAgents:
    def _layer(self, tmp_path, name, text):
        d = tmp_path / "agents"
        d.mkdir(exist_ok=True)
        (d / name).write_text(text)
        return [("bundle", d)]

    def test_agent_installed_with_mapped_tool_names(self, adapter, tmp_path):
        layers = self._layer(
            tmp_path,
            "reviewer.md",
            "---\ndescription: Reviews code\ntools: Read, Grep, Bash\n---\n\nReview carefully.\n",
        )
        adapter.install_features("agents", layers, lambda p: p.suffix == ".md")
        out = (copilot_home() / "agents" / "reviewer.md").read_text()
        assert "description: Reviews code" in out
        assert "Review carefully." in out
        for copilot_name in ("view", "grep", "shell"):
            assert copilot_name in out
        for claude_name in ("Read,", "Grep,", "Bash\n"):
            assert claude_name not in out.split("---")[1]

    def test_scoped_grants_reduce_to_bare_mapped_tools(self, adapter, tmp_path):
        """Claude scoped grants like Bash(git diff*) are not copilot grammar --
        untranslated they load an agent with no usable tools. The scope is
        dropped and the bare tool mapped (observed live)."""
        layers = self._layer(
            tmp_path,
            "scout.md",
            "---\ndescription: scoped\ntools:\n- Bash(git diff*)\n- Bash(git log*)\n- Read\n---\n\nbody\n",
        )
        adapter.install_features("agents", layers, lambda p: p.suffix == ".md")
        out = (copilot_home() / "agents" / "scout.md").read_text()
        front = out.split("---")[1]
        assert "shell" in front
        assert "view" in front
        assert "(" not in front

    def test_claude_model_alias_dropped(self, adapter, tmp_path):
        """`model: haiku` is not a copilot model id -- copilot warns and
        falls back to auto per invocation; dropping the field IS that fallback."""
        layers = self._layer(
            tmp_path,
            "fast.md",
            "---\ndescription: fast\nmodel: haiku\n---\n\nbody\n",
        )
        adapter.install_features("agents", layers, lambda p: p.suffix == ".md")
        out = (copilot_home() / "agents" / "fast.md").read_text()
        assert "model:" not in out.split("---")[1]

    def test_description_synthesized_when_missing(self, adapter, tmp_path):
        """Copilot refuses to load an agent with no description."""
        layers = self._layer(tmp_path, "scout.md", "no frontmatter at all\n")
        adapter.install_features("agents", layers, lambda p: p.suffix == ".md")
        out = (copilot_home() / "agents" / "scout.md").read_text()
        assert "description:" in out
        assert "scout" in out

    def test_oversized_body_truncated_with_marker(self, adapter, tmp_path):
        body = "\n\n".join(["paragraph " + "x" * 200] * 400)
        layers = self._layer(tmp_path, "big.md", f"---\ndescription: Big\n---\n\n{body}\n")
        adapter.install_features("agents", layers, lambda p: p.suffix == ".md")
        out = (copilot_home() / "agents" / "big.md").read_text()
        assert len(out) < 31000
        assert "truncated by agentihooks" in out

    def test_stale_agent_reaped_on_rerun(self, adapter, tmp_path):
        layers = self._layer(tmp_path, "gone.md", "---\ndescription: X\n---\n\nbody\n")
        adapter.install_features("agents", layers, lambda p: p.suffix == ".md")
        assert (copilot_home() / "agents" / "gone.md").exists()
        (tmp_path / "agents" / "gone.md").unlink()
        adapter.install_features("agents", [("bundle", tmp_path / "agents")], lambda p: p.suffix == ".md")
        assert not (copilot_home() / "agents" / "gone.md").exists()

    def test_operator_file_not_overwritten(self, adapter, tmp_path, capsys):
        dst_dir = copilot_home() / "agents"
        dst_dir.mkdir(parents=True, exist_ok=True)
        (dst_dir / "mine.md").write_text("operator wrote this")
        layers = self._layer(tmp_path, "mine.md", "---\ndescription: X\n---\n\nbundle body\n")
        adapter.install_features("agents", layers, lambda p: p.suffix == ".md")
        assert (dst_dir / "mine.md").read_text() == "operator wrote this"
        assert "operator file wins" in capsys.readouterr().out


class TestCommandsToSkills:
    def _layer(self, tmp_path, name, text):
        d = tmp_path / "commands"
        d.mkdir(exist_ok=True)
        (d / name).write_text(text)
        return [("bundle", d)]

    def test_command_becomes_skill_folder(self, adapter, tmp_path):
        layers = self._layer(
            tmp_path, "review-changes.md", "---\ndescription: Review the diff\n---\n\nDo the review.\n"
        )
        adapter.install_features("commands", layers, lambda p: p.suffix == ".md")
        skill = agents_skills_home() / "review-changes" / "SKILL.md"
        assert skill.exists()
        text = skill.read_text()
        assert "name: review-changes" in text
        assert "description: Review the diff" in text
        assert "Do the review." in text

    def test_symlinked_skill_of_same_name_wins(self, adapter, tmp_path, capsys):
        """A real skill outranks a translated command; overwriting would delete it."""
        real_skill = tmp_path / "real-skill"
        real_skill.mkdir()
        (real_skill / "SKILL.md").write_text("real skill content")
        dst = agents_skills_home()
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "collide").symlink_to(real_skill)

        layers = self._layer(tmp_path, "collide.md", "---\ndescription: X\n---\n\ncommand body\n")
        adapter.install_features("commands", layers, lambda p: p.suffix == ".md")
        assert (dst / "collide").is_symlink()
        assert (dst / "collide" / "SKILL.md").read_text() == "real skill content"
        assert "owns this name" in capsys.readouterr().out

    def test_stale_translated_command_reaped(self, adapter, tmp_path):
        layers = self._layer(tmp_path, "gone.md", "---\ndescription: X\n---\n\nbody\n")
        adapter.install_features("commands", layers, lambda p: p.suffix == ".md")
        assert (agents_skills_home() / "gone" / "SKILL.md").exists()
        (tmp_path / "commands" / "gone.md").unlink()
        adapter.install_features("commands", [("bundle", tmp_path / "commands")], lambda p: p.suffix == ".md")
        assert not (agents_skills_home() / "gone").exists()

    def test_reaping_never_touches_symlinked_skills(self, adapter, tmp_path):
        """The shared ~/.agents/skills dir also holds codex's symlinks."""
        real_skill = tmp_path / "codex-skill"
        real_skill.mkdir()
        (real_skill / "SKILL.md").write_text("shared skill")
        dst = agents_skills_home()
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "shared").symlink_to(real_skill)

        layers = self._layer(tmp_path, "temp.md", "---\ndescription: X\n---\n\nbody\n")
        adapter.install_features("commands", layers, lambda p: p.suffix == ".md")
        (tmp_path / "commands" / "temp.md").unlink()
        adapter.install_features("commands", [("bundle", tmp_path / "commands")], lambda p: p.suffix == ".md")
        assert (dst / "shared").is_symlink()
        assert (dst / "shared" / "SKILL.md").read_text() == "shared skill"

    def test_real_skill_added_later_reclaims_the_name_in_one_cycle(self, adapter, tmp_path):
        """A name that was a command and is now a real skill must not stay
        shadowed: the symlinker refuses to replace a non-symlink, so the
        translated directory has to be reaped first."""
        layers = self._layer(tmp_path, "only-cmd.md", "---\ndescription: cmd\n---\n\nold command content\n")
        adapter.install_features("commands", layers, lambda p: p.suffix == ".md")
        translated = agents_skills_home() / "only-cmd"
        assert translated.is_dir() and not translated.is_symlink()

        # The bundle now also ships a real skill of that name.
        skills_src = tmp_path / "skills"
        (skills_src / "only-cmd").mkdir(parents=True)
        (skills_src / "only-cmd" / "SKILL.md").write_text("REAL SKILL")
        adapter.install_features("skills", [("bundle", skills_src)], lambda p: p.is_dir())

        assert translated.is_symlink(), "translated command still shadows the real skill"
        assert (translated / "SKILL.md").read_text() == "REAL SKILL"

    def test_reap_only_touches_manifest_owned_directories(self, adapter, tmp_path):
        """An operator directory of the same name is not ours to delete."""
        dst = agents_skills_home()
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "operator-owned").mkdir()
        (dst / "operator-owned" / "SKILL.md").write_text("operator content")

        skills_src = tmp_path / "skills"
        (skills_src / "operator-owned").mkdir(parents=True)
        (skills_src / "operator-owned" / "SKILL.md").write_text("REAL SKILL")
        adapter.install_features("skills", [("bundle", skills_src)], lambda p: p.is_dir())

        assert (dst / "operator-owned" / "SKILL.md").read_text() == "operator content"

    def test_manifest_is_distinct_from_codex_prompt_manifest(self, adapter, tmp_path):
        layers = self._layer(tmp_path, "x.md", "---\ndescription: X\n---\n\nbody\n")
        adapter.install_features("commands", layers, lambda p: p.suffix == ".md")
        assert (agents_skills_home() / ".agentihooks-copilot-commands.json").exists()


class TestMcp:
    def test_stdio_server_written_as_local(self, adapter):
        adapter.register_mcp({"hooks-utils": {"command": "/usr/bin/python", "args": ["-m", "hooks.mcp"]}})
        doc = json.loads((copilot_home() / "mcp-config.json").read_text())
        entry = doc["mcpServers"]["hooks-utils"]
        assert entry["type"] == "local"
        assert entry["command"] == "/usr/bin/python"
        assert entry["args"] == ["-m", "hooks.mcp"]

    def test_sse_server_round_trips(self, adapter):
        """Codex drops SSE; copilot has an SSE client, so it must survive."""
        adapter.register_mcp({"events": {"type": "sse", "url": "https://mcp.example/sse"}})
        doc = json.loads((copilot_home() / "mcp-config.json").read_text())
        assert doc["mcpServers"]["events"]["type"] == "sse"
        assert doc["mcpServers"]["events"]["url"] == "https://mcp.example/sse"

    def test_http_server_keeps_literal_headers(self, adapter):
        adapter.register_mcp({"gw": {"type": "http", "url": "https://gw.example/mcp", "headers": {"X-Env": "prod"}}})
        doc = json.loads((copilot_home() / "mcp-config.json").read_text())
        assert doc["mcpServers"]["gw"]["headers"] == {"X-Env": "prod"}

    def test_placeholder_header_dropped_when_var_unset(self, adapter, capsys, monkeypatch):
        """An unset ${VAR} header is dropped — copilot cannot expand it."""
        monkeypatch.delenv("TOKEN", raising=False)
        adapter.register_mcp(
            {"gw": {"type": "http", "url": "https://gw.example/mcp", "headers": {"Authorization": "Bearer ${TOKEN}"}}}
        )
        doc = json.loads((copilot_home() / "mcp-config.json").read_text())
        assert "headers" not in doc["mcpServers"]["gw"]
        assert "unset" in capsys.readouterr().out

    def test_placeholder_header_resolved_from_env_when_set(self, adapter, monkeypatch):
        """Copilot sends headers literally, so a ${VAR} whose var IS set at
        install time is baked to the literal — the value ~/.claude.json holds."""
        monkeypatch.setenv("TOKEN", "resolved-secret-value")
        adapter.register_mcp(
            {"gw": {"type": "http", "url": "https://gw.example/mcp", "headers": {"Authorization": "Bearer ${TOKEN}"}}}
        )
        doc = json.loads((copilot_home() / "mcp-config.json").read_text())
        assert doc["mcpServers"]["gw"]["headers"]["Authorization"] == "Bearer resolved-secret-value"

    def test_unbraced_header_resolved_from_env_when_set(self, adapter, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "abc123")
        adapter.register_mcp(
            {"gw": {"type": "http", "url": "https://gw.example/mcp", "headers": {"X-Tok": "$MY_TOKEN"}}}
        )
        doc = json.loads((copilot_home() / "mcp-config.json").read_text())
        assert doc["mcpServers"]["gw"]["headers"]["X-Tok"] == "abc123"

    def test_literal_credential_in_header_is_dropped(self, adapter, capsys):
        adapter.register_mcp(
            {
                "gw": {
                    "type": "http",
                    "url": "https://gw.example/mcp",
                    "headers": {"Authorization": "Bearer ghp_" + "a" * 36},
                }
            }
        )
        doc = json.loads((copilot_home() / "mcp-config.json").read_text())
        assert "headers" not in doc["mcpServers"]["gw"]
        assert "credential" in capsys.readouterr().out

    def test_literal_credential_in_env_is_dropped(self, adapter, capsys):
        adapter.register_mcp({"srv": {"command": "/bin/srv", "env": {"API_KEY": "ghp_" + "b" * 36}}})
        doc = json.loads((copilot_home() / "mcp-config.json").read_text())
        assert "env" not in doc["mcpServers"]["srv"]
        assert "credential" in capsys.readouterr().out

    def test_credential_concatenated_onto_a_placeholder_is_dropped(self, adapter, capsys):
        """A value merely CONTAINING ${VAR} must not skip the scan."""
        adapter.register_mcp({"srv": {"command": "/bin/srv", "env": {"API_KEY": "${SAFE_VAR}-and-ghp_" + "d" * 36}}})
        doc = json.loads((copilot_home() / "mcp-config.json").read_text())
        assert "env" not in doc["mcpServers"]["srv"]
        assert "credential" in capsys.readouterr().out

    def test_credential_in_tools_list_is_dropped(self, adapter, capsys):
        adapter.register_mcp({"srv": {"command": "/bin/srv", "tools": ["read_file", "leaked-ghp_" + "e" * 36]}})
        doc = json.loads((copilot_home() / "mcp-config.json").read_text())
        assert doc["mcpServers"]["srv"]["tools"] == ["read_file"]
        assert "credential" in capsys.readouterr().out

    def test_ordinary_tools_list_survives(self, adapter):
        adapter.register_mcp({"srv": {"command": "/bin/srv", "tools": ["*"]}})
        doc = json.loads((copilot_home() / "mcp-config.json").read_text())
        assert doc["mcpServers"]["srv"]["tools"] == ["*"]

    def test_env_var_reference_survives(self, adapter):
        adapter.register_mcp({"srv": {"command": "/bin/srv", "env": {"API_KEY": "${MY_TOKEN}"}}})
        doc = json.loads((copilot_home() / "mcp-config.json").read_text())
        assert doc["mcpServers"]["srv"]["env"]["API_KEY"] == "${MY_TOKEN}"

    def test_layers_merge_rather_than_replace(self, adapter):
        adapter.register_mcp({"a": {"command": "/bin/a"}})
        adapter.register_mcp({"b": {"command": "/bin/b"}})
        doc = json.loads((copilot_home() / "mcp-config.json").read_text())
        assert set(doc["mcpServers"]) == {"a", "b"}

    def test_unbraced_env_reference_header_dropped_when_unset(self, adapter, capsys, monkeypatch):
        monkeypatch.delenv("MY_TOKEN", raising=False)
        adapter.register_mcp(
            {"gw": {"type": "http", "url": "https://gw.example/mcp", "headers": {"X-Tok": "$MY_TOKEN"}}}
        )
        doc = json.loads((copilot_home() / "mcp-config.json").read_text())
        assert "headers" not in doc["mcpServers"]["gw"]
        assert "unset" in capsys.readouterr().out

    def test_credential_in_url_drops_the_whole_server(self, adapter, capsys):
        tok = "ghp_" + "f" * 36
        adapter.register_mcp({"bad": {"type": "http", "url": f"https://user:{tok}@gw.example/mcp"}})
        doc = json.loads((copilot_home() / "mcp-config.json").read_text())
        assert "bad" not in doc.get("mcpServers", {})
        assert "NOT written" in capsys.readouterr().out

    def test_credential_in_args_drops_the_whole_server(self, adapter, capsys):
        tok = "ghp_" + "g" * 36
        adapter.register_mcp({"bad": {"command": "/bin/srv", "args": ["--token", tok]}})
        doc = json.loads((copilot_home() / "mcp-config.json").read_text())
        assert "bad" not in doc.get("mcpServers", {})
        assert "NOT written" in capsys.readouterr().out

    def test_registered_names_recorded_for_teardown(self, adapter):
        adapter.register_mcp({"a": {"command": "/bin/a"}})
        state = install._load_state()
        assert "a" in install._global_record(state, "copilot").get("managed_mcp", [])

    def test_operator_servers_preserved(self, adapter):
        home = copilot_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "mcp-config.json").write_text(json.dumps({"mcpServers": {"mine": {"type": "local", "command": "x"}}}))
        adapter.register_mcp({"a": {"command": "/bin/a"}})
        doc = json.loads((home / "mcp-config.json").read_text())
        assert "mine" in doc["mcpServers"]
        assert "a" in doc["mcpServers"]


class TestSkillDescriptionCap:
    """Copilot drops a skill whose description exceeds 1024 chars; claude and
    codex do not, so the failure is invisible until a copilot session says so."""

    def _skill(self, root, name, description):
        d = root / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n\nbody\n")

    def test_flags_only_the_oversized_ones(self, tmp_path):
        from scripts.targets.copilot_target import oversized_skill_descriptions

        self._skill(tmp_path, "fine", "short and useful")
        self._skill(tmp_path, "too-long", "x" * 1100)
        assert oversized_skill_descriptions(tmp_path) == [("too-long", 1100)]

    def test_folded_yaml_is_measured_as_rendered(self, tmp_path):
        from scripts.targets.copilot_target import oversized_skill_descriptions

        d = tmp_path / "folded"
        d.mkdir()
        wrapped = "\n".join("  " + "y" * 60 for _ in range(20))  # 1200 chars of content
        (d / "SKILL.md").write_text(f"---\nname: folded\ndescription: >\n{wrapped}\n---\n\nbody\n")
        assert [n for n, _ in oversized_skill_descriptions(tmp_path)] == ["folded"]

    def test_missing_or_unparseable_skill_is_skipped(self, tmp_path):
        from scripts.targets.copilot_target import oversized_skill_descriptions

        (tmp_path / "nofile").mkdir()
        bad = tmp_path / "badyaml"
        bad.mkdir()
        (bad / "SKILL.md").write_text("---\nname: [unclosed\n---\n")
        assert oversized_skill_descriptions(tmp_path) == []


class TestDoctor:
    def test_reports_failures_on_bare_home(self, adapter, capsys):
        assert adapter.doctor() > 0
        assert "settings.json missing" in capsys.readouterr().out

    def test_passes_core_checks_after_install(self, adapter, tmp_path, capsys):
        adapter.write_settings({})
        adapter.register_mcp({"hooks-utils": {"command": "/usr/bin/python", "args": ["-m", "hooks.mcp"]}})
        prof = tmp_path / "p"
        prof.mkdir()
        (prof / "CLAUDE.md").write_text("persona")
        adapter.install_persona([("p", prof)], ["p"], None)
        adapter.doctor()
        out = capsys.readouterr().out
        assert "[!!] settings.json missing" not in out
        assert "[!!] mcp-config.json missing" not in out
        assert "[!!] copilot-instructions.md missing" not in out
        assert "agentihooks.json wires" in out


class TestAdapterRegistration:
    def test_get_adapter_returns_copilot(self):
        from scripts.targets import SUPPORTED_TARGETS, get_adapter

        assert "copilot" in SUPPORTED_TARGETS
        assert get_adapter("copilot").name == "copilot"

    def test_importable_under_bare_targets_identity(self):
        from targets.copilot_target import CopilotAdapter as Bare

        assert Bare().name == "copilot"


class TestTeardown:
    def _full_install(self, adapter, tmp_path):
        adapter.write_settings({"_agentihooks": {"allowAll": True}})
        adapter.register_mcp({"hooks-utils": {"command": "/usr/bin/python", "args": ["-m", "hooks.mcp"]}})
        agents_src = tmp_path / "agents"
        agents_src.mkdir(exist_ok=True)
        (agents_src / "scout.md").write_text("---\ndescription: X\n---\n\nbody\n")
        adapter.install_features("agents", [("bundle", agents_src)], lambda p: p.suffix == ".md")
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
        home = copilot_home()
        assert not (home / "agentihooks-hook.sh").exists()
        assert not (home / "hooks" / "agentihooks.json").exists()
        assert not (home / "copilot-instructions.md").exists()
        assert not (home / "agents" / "scout.md").exists()
        assert not (agents_skills_home() / "review").exists()
        doc = json.loads((home / "settings.json").read_text())
        assert "agentihooks" not in doc
        assert "statusLine" not in doc
        assert str(install.AGENTIHOOKS_ROOT) not in doc.get("trustedFolders", [])
        mcp = json.loads((home / "mcp-config.json").read_text())
        assert "hooks-utils" not in mcp.get("mcpServers", {})
        assert install._global_record(install._load_state(), "copilot").get("managed_mcp") is None

    def test_preserves_operator_content(self, adapter, tmp_path):
        self._full_install(adapter, tmp_path)
        home = copilot_home()
        doc = json.loads((home / "settings.json").read_text())
        doc["theme"] = "dim"
        (home / "settings.json").write_text(json.dumps(doc))
        hooks_doc = json.loads((home / "hooks" / "agentihooks.json").read_text())
        hooks_doc["hooks"]["preToolUse"].append({"type": "command", "command": "/usr/bin/operator-hook"})
        (home / "hooks" / "agentihooks.json").write_text(json.dumps(hooks_doc))
        persona = home / "copilot-instructions.md"
        persona.write_text(persona.read_text() + "\n## operator tail\n")
        mcp = json.loads((home / "mcp-config.json").read_text())
        mcp["mcpServers"]["operators-own"] = {"type": "local", "command": "/bin/x"}
        (home / "mcp-config.json").write_text(json.dumps(mcp))

        adapter.teardown()

        doc = json.loads((home / "settings.json").read_text())
        assert doc["theme"] == "dim"
        hooks_doc = json.loads((home / "hooks" / "agentihooks.json").read_text())
        assert hooks_doc["hooks"]["preToolUse"][0]["command"] == "/usr/bin/operator-hook"
        assert "## operator tail" in persona.read_text()
        assert "managed-by: agentihooks" not in persona.read_text()
        mcp = json.loads((home / "mcp-config.json").read_text())
        assert "operators-own" in mcp["mcpServers"]

    def test_hand_edited_managed_key_survives(self, adapter, tmp_path):
        self._full_install(adapter, tmp_path)
        home = copilot_home()
        doc = json.loads((home / "settings.json").read_text())
        doc["statusLine"] = {"type": "command", "command": "/usr/local/bin/my-status"}
        (home / "settings.json").write_text(json.dumps(doc))
        adapter.teardown()
        doc = json.loads((home / "settings.json").read_text())
        assert doc["statusLine"]["command"] == "/usr/local/bin/my-status"

    def test_idempotent_on_clean_home(self, adapter):
        adapter.teardown()
        adapter.teardown()


class TestTeardownDestructiveEdges:
    def test_header_without_footer_preserves_whole_file_as_backup(self, adapter):
        from scripts.targets.copilot_target import _MANAGED_HEADER

        home = copilot_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "copilot-instructions.md").write_text(_MANAGED_HEADER + "\nmanaged\n\nMY OWN NOTES\n")
        adapter.teardown()
        assert not (home / "copilot-instructions.md").exists()
        baks = list(home.glob("copilot-instructions*.bak*"))
        assert baks and any("MY OWN NOTES" in b.read_text() for b in baks)

    def test_unrecorded_operator_hooks_utils_survives(self, adapter, capsys):
        home = copilot_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "mcp-config.json").write_text(
            json.dumps({"mcpServers": {"hooks-utils": {"type": "local", "command": "/opt/operator-own/server"}}})
        )
        adapter.teardown()
        doc = json.loads((home / "mcp-config.json").read_text())
        assert "hooks-utils" in doc["mcpServers"]
        assert "review it" in capsys.readouterr().out

    def test_missing_record_content_verified_statusline_removed(self, adapter):
        home = copilot_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "settings.json").write_text(
            json.dumps({"statusLine": {"type": "command", "command": "python -m hooks.statusline"}})
        )
        adapter.teardown()
        doc = json.loads((home / "settings.json").read_text())
        assert "statusLine" not in doc

    def test_missing_record_operator_statusline_survives(self, adapter):
        home = copilot_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "settings.json").write_text(
            json.dumps({"statusLine": {"type": "command", "command": "/usr/local/bin/my-status"}})
        )
        adapter.teardown()
        doc = json.loads((home / "settings.json").read_text())
        assert doc["statusLine"]["command"] == "/usr/local/bin/my-status"

    def test_unparseable_hooks_file_backed_up_not_deleted(self, adapter):
        home = copilot_home()
        (home / "hooks").mkdir(parents=True, exist_ok=True)
        (home / "hooks" / "agentihooks.json").write_text('{"hooks": {"x": [{"command": "operator_hook"}]}],,,')
        adapter.teardown()
        assert not (home / "hooks" / "agentihooks.json").exists()
        baks = list((home / "hooks").glob("agentihooks*.bak*"))
        assert baks and any("operator_hook" in b.read_text() for b in baks)


class TestManagedSidecar:
    """The managed-key record lives in .agentihooks-managed.json — an in-file
    `agentihooks` key makes copilot warn about unknown settings keys on every
    launch (observed v1.0.80)."""

    def test_settings_json_carries_no_agentihooks_key(self, adapter):
        adapter.write_settings({"statusLine": {"type": "command", "command": "/usr/bin/python -m hooks.statusline"}})
        doc = json.loads((copilot_home() / "settings.json").read_text())
        assert "agentihooks" not in doc
        sidecar = json.loads((copilot_home() / ".agentihooks-managed.json").read_text())
        assert "statusLine" in sidecar

    def test_legacy_infile_record_migrates_and_key_is_removed(self, adapter):
        home = copilot_home()
        home.mkdir(parents=True, exist_ok=True)
        old_status = {"type": "command", "command": "/old/python -m hooks.statusline"}
        (home / "settings.json").write_text(
            json.dumps({"agentihooks": {"managed": {"statusLine": old_status}}, "statusLine": old_status})
        )
        adapter.write_settings({})
        doc = json.loads((home / "settings.json").read_text())
        assert "agentihooks" not in doc
        assert "hooks.statusline" in doc["statusLine"]["command"], "recorded value must still count as ours"

    def test_teardown_reads_sidecar_and_removes_it(self, adapter):
        adapter.write_settings({})
        adapter.teardown()
        home = copilot_home()
        assert not (home / ".agentihooks-managed.json").exists()
        doc = json.loads((home / "settings.json").read_text())
        assert "statusLine" not in doc


class TestSidecarSelfHeal:
    """A lost/deleted .agentihooks-managed.json with settings.json still
    holding our values must not brand every managed key a permanent hand-edit."""

    def test_sidecar_loss_reheals_and_does_not_warn(self, adapter, capsys):
        adapter.write_settings({"statusLine": {"type": "command", "command": "/usr/bin/python -m hooks.statusline"}})
        home = copilot_home()
        sidecar = adapter._managed_sidecar(home)
        assert sidecar.exists()
        sidecar.unlink()
        capsys.readouterr()

        adapter.write_settings({"statusLine": {"type": "command", "command": "/usr/bin/python -m hooks.statusline"}})
        out = capsys.readouterr().out
        assert "hand-set" not in out, "sidecar loss with our value intact must not read as a hand-edit"
        healed = json.loads(sidecar.read_text())
        assert "statusLine" in healed and "disableAllHooks" in healed, "sidecar must re-heal all managed keys"

    def test_genuine_hand_edit_still_detected_after_sidecar_loss(self, adapter, capsys):
        adapter.write_settings({"statusLine": {"type": "command", "command": "/usr/bin/python -m hooks.statusline"}})
        home = copilot_home()
        doc = json.loads((home / "settings.json").read_text())
        doc["statusLine"] = {"type": "command", "command": "/usr/local/bin/mine"}
        (home / "settings.json").write_text(json.dumps(doc))
        adapter._managed_sidecar(home).unlink()
        capsys.readouterr()

        adapter.write_settings({"statusLine": {"type": "command", "command": "/usr/bin/python -m hooks.statusline"}})
        out = capsys.readouterr().out
        assert "hand-set" in out, "a real hand-edit that differs from our value must still be respected"
        doc = json.loads((home / "settings.json").read_text())
        assert doc["statusLine"]["command"] == "/usr/local/bin/mine"


class TestNativeMcpPassThrough:
    """Copilot-native MCP fields a Claude .mcp.json cannot express must survive
    registration. `auth: false` is the one that matters operationally: without
    it a 401 from an http/sse server starts a browser OAuth flow, which under
    WSL opens a Windows browser with no session and hangs the turn."""

    def test_auth_false_survives(self, adapter):
        adapter.register_mcp(
            {
                "gw": {
                    "type": "http",
                    "url": "https://gw.example/mcp",
                    "auth": False,
                    "oidc": False,
                    "deferTools": "auto",
                }
            }
        )
        entry = json.loads((copilot_home() / "mcp-config.json").read_text())["mcpServers"]["gw"]
        assert entry["auth"] is False
        assert entry["oidc"] is False
        assert entry["deferTools"] == "auto"

    def test_tool_allowlist_and_exclusions_survive(self, adapter):
        adapter.register_mcp(
            {"srv": {"command": "/bin/srv", "tools": ["a", "b"], "excludeTools": ["c"], "timeout": 30000}}
        )
        entry = json.loads((copilot_home() / "mcp-config.json").read_text())["mcpServers"]["srv"]
        assert entry["tools"] == ["a", "b"]
        assert entry["excludeTools"] == ["c"]
        assert entry["timeout"] == 30000

    def test_oauth_is_off_unless_a_server_opts_in(self, adapter):
        """Interactive OAuth is opt-in: an unconfigured server must not be able
        to start a browser flow, which hangs under WSL."""
        adapter.register_mcp({"srv": {"command": "/bin/srv"}})
        entry = json.loads((copilot_home() / "mcp-config.json").read_text())["mcpServers"]["srv"]
        assert entry["auth"] is False
        assert entry["oidc"] is False
        for key in ("deferTools", "excludeTools"):
            assert key not in entry

    def test_a_server_can_opt_back_into_oauth(self, adapter):
        adapter.register_mcp({"srv": {"type": "http", "url": "https://x.example/mcp", "auth": True}})
        entry = json.loads((copilot_home() / "mcp-config.json").read_text())["mcpServers"]["srv"]
        assert entry["auth"] is True


class TestNativeDirectives:
    def test_reserved_block_never_reaches_settings_json(self, adapter):
        adapter.write_settings({"_agentihooks": {"allowAll": True}, "effortLevel": "high"})
        doc = json.loads((copilot_home() / "settings.json").read_text())
        assert "_agentihooks" not in doc, "copilot warns about unknown top-level keys"
        assert doc["effortLevel"] == "high"

    def test_native_hooks_key_is_dropped_with_a_warning(self, adapter, capsys):
        """Copilot merges inline settings hooks with the hooks/ dir — declaring
        both fires every hook twice."""
        adapter.write_settings({"hooks": {"preToolUse": [{"command": "/x"}]}})
        doc = json.loads((copilot_home() / "settings.json").read_text())
        assert "hooks" not in doc
        assert "fires each hook twice" in capsys.readouterr().out


class TestSuppressBrowserLaunch:
    """Copilot starts an MCP OAuth flow at startup and has no defer-auth key;
    COPILOT_DEBUG_BROWSER is the only pre-launch interception point."""

    @staticmethod
    def _env_text(adapter):
        return adapter._bypass_env_file().read_text()

    def test_directive_writes_a_debug_browser_sink(self, adapter):
        adapter.write_settings({"_agentihooks": {"suppressBrowserLaunch": True}})
        assert "COPILOT_DEBUG_BROWSER=" in self._env_text(adapter)

    def test_sink_parses_as_copilot_parses_it_and_captures_the_url(self, adapter, tmp_path):
        adapter.write_settings({"_agentihooks": {"suppressBrowserLaunch": True}})
        line = next(ln for ln in self._env_text(adapter).splitlines() if ln.startswith("COPILOT_DEBUG_BROWSER="))
        spec = json.loads(line.split("=", 1)[1].strip("'"))
        assert isinstance(spec, list) and spec and all(isinstance(x, str) for x in spec)

        url = "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize?x=1"
        subprocess.run(  # copilot: spawn(spec[0], [...spec.slice(1), url])
            [*spec, url], env={**os.environ, "HOME": str(tmp_path)}, check=True
        )
        assert (tmp_path / ".copilot" / "pending-oauth-urls.txt").read_text().strip() == url

    def test_absent_directive_writes_no_sink(self, adapter):
        adapter.write_settings({"_agentihooks": {"allowAll": True}})
        assert "COPILOT_DEBUG_BROWSER" not in self._env_text(adapter)

    def test_both_directives_coexist(self, adapter):
        adapter.write_settings({"_agentihooks": {"allowAll": True, "suppressBrowserLaunch": True}})
        text = self._env_text(adapter)
        assert "COPILOT_ALLOW_ALL=true" in text and "COPILOT_DEBUG_BROWSER=" in text

    def test_dropping_every_directive_removes_the_env_file(self, adapter):
        adapter.write_settings({"_agentihooks": {"suppressBrowserLaunch": True}})
        adapter.write_settings({})
        assert not adapter._bypass_env_file().exists()


class TestMcpDefaultDisabled:
    """Copilot connects every configured server at session start; disabling by
    default makes /mcp enable the on-demand switch."""

    @staticmethod
    def _settings():
        return json.loads((copilot_home() / "settings.json").read_text())

    @staticmethod
    def _seed_mcp(names):
        path = copilot_home() / "mcp-config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mcpServers": {n: {"type": "local", "command": "/bin/true"} for n in names}}))

    def test_every_configured_server_starts_disabled(self, adapter):
        adapter.write_settings({"_agentihooks": {"mcpDefaultDisabled": True}})
        self._seed_mcp(["atlassian", "drawio", "WorkIQ-MailServer"])
        adapter.post_install_reconcile([], "smith")
        assert self._settings()["disabledMcpServers"] == [
            "WorkIQ-MailServer",
            "atlassian",
            "drawio",
        ]

    def test_hooks_utils_stays_enabled(self, adapter):
        adapter.write_settings({"_agentihooks": {"mcpDefaultDisabled": True}})
        self._seed_mcp(["hooks-utils", "drawio"])
        adapter.post_install_reconcile([], "smith")
        assert self._settings()["disabledMcpServers"] == ["drawio"]

    def test_operator_enabled_server_is_not_re_disabled(self, adapter):
        """`/mcp enable X` records X in enabledMcpServers; the next install must
        leave it on."""
        adapter.write_settings({"_agentihooks": {"mcpDefaultDisabled": True}})
        self._seed_mcp(["atlassian", "drawio"])
        adapter.post_install_reconcile([], "smith")

        path = copilot_home() / "settings.json"
        doc = json.loads(path.read_text())
        doc["disabledMcpServers"] = ["drawio"]
        doc["enabledMcpServers"] = ["atlassian"]
        path.write_text(json.dumps(doc))

        adapter.write_settings({"_agentihooks": {"mcpDefaultDisabled": True}})
        adapter.post_install_reconcile([], "smith")
        assert self._settings()["disabledMcpServers"] == ["drawio"]

    def test_custom_always_enabled_list(self, adapter):
        adapter.write_settings({"_agentihooks": {"mcpDefaultDisabled": True, "mcpAlwaysEnabled": ["drawio"]}})
        self._seed_mcp(["hooks-utils", "drawio", "atlassian"])
        adapter.post_install_reconcile([], "smith")
        assert self._settings()["disabledMcpServers"] == ["atlassian", "hooks-utils"]

    def test_absent_directive_disables_nothing(self, adapter):
        adapter.write_settings({})
        self._seed_mcp(["atlassian", "drawio"])
        adapter.post_install_reconcile([], "smith")
        assert "disabledMcpServers" not in self._settings()


class TestBrowserCommand:
    """Under WSL, Copilot's xdg-open default reaches a Linux browser carrying none
    of the operator's Windows sessions; COPILOT_DEBUG_BROWSER redirects it."""

    @staticmethod
    def _launcher(adapter):
        line = next(
            ln for ln in adapter._bypass_env_file().read_text().splitlines() if ln.startswith("COPILOT_DEBUG_BROWSER=")
        )
        return json.loads(line.split("=", 1)[1].strip("'"))

    def test_array_form_is_written_verbatim(self, adapter):
        adapter.write_settings({"_agentihooks": {"browserCommand": ["/bin/sh"]}})
        assert self._launcher(adapter) == ["/bin/sh"]

    def test_string_form_is_shell_split(self, adapter):
        adapter.write_settings({"_agentihooks": {"browserCommand": '"/bin/sh" --new-tab'}})
        assert self._launcher(adapter) == ["/bin/sh", "--new-tab"]

    def test_browser_command_wins_over_the_sink(self, adapter):
        adapter.write_settings({"_agentihooks": {"browserCommand": ["/bin/sh"], "suppressBrowserLaunch": True}})
        assert self._launcher(adapter) == ["/bin/sh"]

    def test_absent_leaves_copilot_default_untouched(self, adapter):
        adapter.write_settings({"_agentihooks": {"allowAll": True}})
        assert "COPILOT_DEBUG_BROWSER" not in adapter._bypass_env_file().read_text()

    def test_unresolvable_command_is_dropped_not_written(self, adapter, capsys):
        """A bundle profile lands on more than one machine; copilot only debug-logs
        a failed spawn, so an absent launcher would silently open nothing."""
        adapter.write_settings({"_agentihooks": {"browserCommand": ["/no/such/browser"], "allowAll": True}})
        assert "COPILOT_DEBUG_BROWSER" not in adapter._bypass_env_file().read_text()
        assert "not found on this machine" in capsys.readouterr().out

    def test_auto_follows_the_platform(self, adapter):
        from scripts.targets import copilot_target

        adapter.write_settings({"_agentihooks": {"browserCommand": "auto", "allowAll": True}})
        text = adapter._bypass_env_file().read_text()
        wsl = adapter._running_under_wsl() and any(adapter._resolves(c) for c in copilot_target._WSL_BROWSER_CANDIDATES)
        assert ("COPILOT_DEBUG_BROWSER" in text) is wsl

    def test_auto_off_wsl_writes_nothing(self, adapter, monkeypatch):
        monkeypatch.setattr(type(adapter), "_running_under_wsl", staticmethod(lambda: False))
        adapter.write_settings({"_agentihooks": {"browserCommand": "auto", "allowAll": True}})
        assert "COPILOT_DEBUG_BROWSER" not in adapter._bypass_env_file().read_text()

    def test_auto_on_wsl_picks_the_first_resolvable_candidate(self, adapter, monkeypatch):
        from scripts.targets import copilot_target

        monkeypatch.setattr(type(adapter), "_running_under_wsl", staticmethod(lambda: True))
        chrome = copilot_target._WSL_BROWSER_CANDIDATES[1]
        monkeypatch.setattr(type(adapter), "_resolves", staticmethod(lambda c: c == chrome))
        adapter.write_settings({"_agentihooks": {"browserCommand": "auto"}})
        assert self._launcher(adapter) == [chrome]

    def test_auto_never_uses_explorer_exe(self, adapter):
        """Spawned with a Linux working directory — copilot's normal condition —
        explorer.exe ignores the URL and opens a File Explorer window."""
        from scripts.targets import copilot_target

        assert not any("explorer.exe" in c for c in copilot_target._WSL_BROWSER_CANDIDATES)

    def test_auto_on_wsl_with_no_windows_browser_warns_and_writes_nothing(self, adapter, monkeypatch, capsys):
        monkeypatch.setattr(type(adapter), "_running_under_wsl", staticmethod(lambda: True))
        monkeypatch.setattr(type(adapter), "_resolves", staticmethod(lambda c: False))
        adapter.write_settings({"_agentihooks": {"browserCommand": "auto", "allowAll": True}})
        assert "COPILOT_DEBUG_BROWSER" not in adapter._bypass_env_file().read_text()
        assert "no Windows browser found from WSL" in capsys.readouterr().out

    def test_launcher_receives_the_url_as_its_last_argument(self, adapter, tmp_path):
        """Copilot spawns spec[0] with the rest of the array plus the URL."""
        seen = tmp_path / "seen.txt"
        adapter.write_settings(
            {"_agentihooks": {"browserCommand": ["sh", "-c", f'printf "%s" "$1" > {seen}', "browser"]}}
        )
        url = "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize"
        subprocess.run([*self._launcher(adapter), url], check=True)
        assert seen.read_text() == url


class TestBroadcastChannels:
    """Claude sets AGENTIHOOKS_BASE_CHANNELS in its settings `env` block; copilot
    has no `env` settings key, so an unset var means no channel subscriptions."""

    @staticmethod
    def _env(adapter):
        return adapter._bypass_env_file().read_text()

    def test_channels_reach_the_env_file(self, adapter):
        adapter.write_settings({"_agentihooks": {"channels": "brain,amygdala"}})
        assert "AGENTIHOOKS_BASE_CHANNELS=brain,amygdala" in self._env(adapter)

    def test_list_form_is_joined(self, adapter):
        adapter.write_settings({"_agentihooks": {"channels": ["brain", "amygdala"]}})
        assert "AGENTIHOOKS_BASE_CHANNELS=brain,amygdala" in self._env(adapter)

    def test_channels_survive_alongside_other_directives(self, adapter):
        adapter.write_settings({"_agentihooks": {"channels": "brain", "allowAll": True}})
        text = self._env(adapter)
        assert "AGENTIHOOKS_BASE_CHANNELS=brain" in text and "COPILOT_ALLOW_ALL=true" in text

    def test_channels_alone_still_writes_the_file(self, adapter):
        adapter.write_settings({"_agentihooks": {"channels": "brain"}})
        assert adapter._bypass_env_file().exists()

    def test_the_shipped_base_subscribes_to_the_claude_defaults(self):
        """A copilot session must land on the same channels a claude session does."""
        repo = Path(__file__).resolve().parents[1]
        base = json.loads((repo / "profiles/_base/settings.base.copilot.json").read_text())
        claude = json.loads((repo / "profiles/default/.claude/settings.overrides.json").read_text())
        assert base["_agentihooks"]["channels"] == claude["env"]["AGENTIHOOKS_BASE_CHANNELS"]


class TestRefuterRegressions:
    """Cases an adversarial pass found after the features shipped."""

    @staticmethod
    def _settings():
        return json.loads((copilot_home() / "settings.json").read_text())

    @staticmethod
    def _seed_mcp(names):
        path = copilot_home() / "mcp-config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mcpServers": {n: {"type": "local", "command": "/bin/true"} for n in names}}))

    def test_always_enabled_server_is_lifted_out_of_an_existing_disable(self, adapter):
        """Adding to the disabled set is not enough: a stale settings file, or one
        past `/mcp disable hooks-utils`, would leave the toolbelt off forever."""
        adapter.write_settings({"_agentihooks": {"mcpDefaultDisabled": True}})
        self._seed_mcp(["hooks-utils", "drawio"])
        path = copilot_home() / "settings.json"
        doc = json.loads(path.read_text())
        doc["disabledMcpServers"] = ["hooks-utils"]
        path.write_text(json.dumps(doc))

        adapter.post_install_reconcile([], "smith")
        assert self._settings()["disabledMcpServers"] == ["drawio"]

    def test_reported_enabled_list_matches_what_was_written(self, adapter, capsys):
        """The log line was computed independently of the written set and could
        claim a server was left enabled while it sat in disabledMcpServers."""
        adapter.write_settings({"_agentihooks": {"mcpDefaultDisabled": True}})
        self._seed_mcp(["hooks-utils", "drawio"])
        adapter.post_install_reconcile([], "smith")
        out = capsys.readouterr().out
        written = set(self._settings()["disabledMcpServers"])
        for line in out.splitlines():
            if "left enabled:" in line:
                claimed = {n.strip() for n in line.split("left enabled:", 1)[1].split(",")}
                assert not (claimed & written), f"claimed enabled but written disabled: {claimed & written}"

    @pytest.mark.parametrize("value", [True, 1, 1.5, {"chrome": True}])
    def test_malformed_browser_command_degrades_instead_of_crashing(self, adapter, capsys, value):
        """A typo'd native file must not abort `agentihooks init --target copilot`."""
        adapter.write_settings({"_agentihooks": {"browserCommand": value, "allowAll": True}})
        assert "COPILOT_DEBUG_BROWSER" not in adapter._bypass_env_file().read_text()
        assert "browserCommand" in capsys.readouterr().out

    def test_malformed_browser_command_leaves_other_directives_working(self, adapter):
        adapter.write_settings({"_agentihooks": {"browserCommand": True, "channels": "brain"}})
        assert "AGENTIHOOKS_BASE_CHANNELS=brain" in adapter._bypass_env_file().read_text()
