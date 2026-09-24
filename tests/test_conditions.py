"""Conditions: filename grammar, layered discovery, the index cache, the script
contract, result merging, and the envelopes hook_manager emits per target."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hooks.hook_manager as hm
from hooks.context import conditions, profile_chain
from hooks.context.conditions import _cache_path as REAL_CACHE_PATH
from hooks.hook_manager import BlockAction
from hooks.targets import emitter

pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _age(*paths: Path) -> None:
    past = time.time() - 60
    for path in paths:
        os.utime(path, (past, past))


def _write(directory: Path, name: str, body: str, mode: int = 0o644) -> Path:
    path = directory / name
    path.write_text(body)
    path.chmod(mode)
    return path


def _py(body: str) -> str:
    return "import json, os, sys\npayload = json.load(sys.stdin)\n" + body


def _state(bundle: Path, profile: str = "alpha", target: str = "claude") -> None:
    state = profile_chain.state_path()
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps({"bundle": {"path": str(bundle)}, "targets": {"global": {target: {"profile": profile}}}})
    )


@pytest.fixture
def layers(tmp_path, monkeypatch):
    """A linked bundle whose chain is the single profile ``alpha``; conditions on."""
    bundle = tmp_path / "bundle"
    global_dir = bundle / ".claude" / "conditions"
    profile_dir = bundle / "profiles" / "alpha" / ".claude" / "conditions"
    global_dir.mkdir(parents=True)
    profile_dir.mkdir(parents=True)
    _state(bundle)
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    monkeypatch.setattr("hooks.config.CONDITIONS_ENABLED", True)
    return bundle, global_dir, profile_dir


def _bash(command: str, mode: str = "bypassPermissions", cwd: str = "/tmp", **extra) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "sid-conditions",
        "tool_name": "Bash",
        "tool_input": {"command": command, "description": "probe"},
        "permission_mode": mode,
        "cwd": cwd,
        **extra,
    }


class TestFilename:
    @pytest.mark.parametrize(
        "name, step, matcher, cname, is_async",
        [
            ("pre-bash-ls_formatter.sh", "pre", "bash", "ls_formatter", False),
            ("post-any-trim.py", "post", "any", "trim", False),
            ("pre-bash.git-guard.sh", "pre", "bash.git", "guard", False),
            ("pre-edit+write-lint.sh", "pre", "edit+write", "lint", False),
            ("post-mcp-audit.async.py", "post", "mcp", "audit", True),
            (
                "pre-mcp__gateway-tools__github-create_pull_request-audit.py",
                "pre",
                "mcp__gateway-tools__github-create_pull_request",
                "audit",
                False,
            ),
        ],
    )
    def test_parses(self, name, step, matcher, cname, is_async):
        meta = conditions.parse_filename(name)
        assert (meta["step"], meta["matcher"], meta["name"], meta["async"]) == (step, matcher, cname, is_async)

    @pytest.mark.parametrize(
        "name, error",
        [
            ("pre-bash.git-audit", "extension"),
            ("pre-bash.sh", "expected"),
            ("stop-bash-x.sh", "unknown step"),
            ("pre-bad*-x.sh", "invalid characters"),
            ("pre--x.sh", "empty matcher"),
        ],
    )
    def test_rejects(self, name, error):
        with pytest.raises(ValueError, match=error):
            conditions.parse_filename(name)

    @pytest.mark.parametrize("name", ["README.md", ".hidden.sh", "_draft.sh", "pre-bash-x.sh~"])
    def test_ignored(self, name):
        assert conditions.is_ignored(name)


class TestLayers:
    def test_profile_overrides_bundle_by_stem(self, layers):
        _, global_dir, profile_dir = layers
        _write(global_dir, "pre-bash-same.sh", "echo bundle")
        _write(profile_dir, "pre-bash-same.sh", "echo profile")
        _write(global_dir, "pre-any-other.sh", "echo other")
        found = conditions.matching("pre", "Bash", {"command": "ls"})
        assert [(e["file"], e["source"]) for e in found] == [
            ("pre-any-other.sh", "bundle"),
            ("pre-bash-same.sh", "profile:alpha"),
        ]

    def test_invalid_files_are_reported_not_indexed(self, layers):
        _, global_dir, _ = layers
        _write(global_dir, "pre-bash.sh", "echo")
        _write(global_dir, "README.md", "docs")
        entries, invalid = conditions.scan_layers(conditions.layer_dirs(profile_chain.read_state())[0])
        assert entries == []
        assert [Path(i["path"]).name for i in invalid] == ["pre-bash.sh"]

    def test_bucket_lookup(self, layers):
        _, global_dir, _ = layers
        for name in (
            "pre-bash.git-g.sh",
            "pre-mcp-m.sh",
            "pre-mcp__srv-s.sh",
            "pre-read-r.sh",
            "pre-edit+bash.git-e.sh",
        ):
            _write(global_dir, name, "echo")

        def names(tool, command=""):
            return [e["file"] for e in conditions.matching("pre", tool, {"command": command})]

        assert names("Bash", "cd x && git status") == ["pre-bash.git-g.sh", "pre-edit+bash.git-e.sh"]
        assert names("Bash", "ls") == []
        assert names("mcp__srv__tool") == ["pre-mcp-m.sh", "pre-mcp__srv-s.sh"]
        assert names("mcp__other__tool") == ["pre-mcp-m.sh"]
        assert names("Read") == ["pre-read-r.sh"]
        assert names("Edit") == ["pre-edit+bash.git-e.sh"]


class TestIndexCache:
    def _prime(self, layers):
        bundle, global_dir, profile_dir = layers
        _write(global_dir, "pre-bash-one.sh", "echo")
        _age(profile_chain.state_path(), global_dir, profile_dir)
        conditions.load_index()
        assert conditions._cache_path().exists()

    def test_hit_skips_the_scan(self, layers, monkeypatch):
        self._prime(layers)
        monkeypatch.setattr(conditions, "scan_layers", lambda *a: (_ for _ in ()).throw(AssertionError("scanned")))
        assert [e["file"] for e in conditions.matching("pre", "Bash", {})] == ["pre-bash-one.sh"]

    def test_new_file_invalidates(self, layers):
        self._prime(layers)
        _write(layers[1], "pre-bash-two.sh", "echo")
        assert [e["file"] for e in conditions.matching("pre", "Bash", {})] == ["pre-bash-one.sh", "pre-bash-two.sh"]

    def test_profile_chain_change_invalidates(self, layers):
        bundle, _, _ = layers
        self._prime(layers)
        beta = bundle / "profiles" / "beta" / ".claude" / "conditions"
        beta.mkdir(parents=True)
        _write(beta, "pre-bash-beta.sh", "echo")
        _state(bundle, profile="beta")
        assert [e["file"] for e in conditions.matching("pre", "Bash", {})] == ["pre-bash-one.sh", "pre-bash-beta.sh"]

    def test_profile_dir_appearing_invalidates(self, layers):
        bundle, _, _ = layers
        _state(bundle, profile="alpha,gamma")
        self._prime(layers)
        gamma = bundle / "profiles" / "gamma" / ".claude" / "conditions"
        gamma.mkdir(parents=True)
        _write(gamma, "pre-bash-gamma.sh", "echo")
        assert "pre-bash-gamma.sh" in [e["file"] for e in conditions.matching("pre", "Bash", {})]

    def test_fresh_mtimes_are_not_cached(self, layers):
        _write(layers[1], "pre-bash-one.sh", "echo")
        conditions.load_index()
        assert not conditions._cache_path().exists()

    def test_unparsable_state_is_not_cached(self, layers):
        profile_chain.state_path().write_text("{")
        _age(profile_chain.state_path())
        assert conditions.load_index()["pre"]["any"] == []
        assert not conditions._cache_path().exists()

    def test_cache_file_is_per_target(self, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
        claude = REAL_CACHE_PATH()
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
        assert REAL_CACHE_PATH() != claude
        assert REAL_CACHE_PATH().name.startswith("conditions-index.codex.")


class TestScriptContract:
    def test_stdin_and_env(self, layers):
        _, _, profile_dir = layers
        _write(
            profile_dir,
            "pre-bash-probe.py",
            _py(
                'print(json.dumps({"context": "|".join([os.environ["AH_STEP"], os.environ["AH_EVENT"], '
                'os.environ["AH_TOOL_NAME"], os.environ["AH_CONDITION"], os.environ["AH_CONDITION_SOURCE"], '
                'os.environ["AH_PERMISSION_MODE"], payload["tool_input"]["command"], os.getcwd()])}))\n'
            ),
        )
        result = conditions.run_step("pre", _bash("ls", cwd=str(layers[0])))
        assert result.contexts == [
            f"[condition pre-bash-probe.py]\npre|PreToolUse|Bash|pre-bash-probe.py|profile:alpha|bypassPermissions|ls|{layers[0]}"
        ]

    def test_plain_text_is_context(self, layers):
        _write(layers[1], "pre-bash-say.sh", "echo hello there")
        assert conditions.run_step("pre", _bash("ls")).contexts == ["[condition pre-bash-say.sh]\nhello there"]

    def test_exit_2_denies_with_stderr(self, layers):
        _write(layers[1], "pre-bash-no.sh", "echo 'not this one' >&2; exit 2")
        result = conditions.run_step("pre", _bash("ls"))
        assert result.decision == "deny"
        assert "not this one" in result.reasons[0]

    def test_other_exit_fails_open(self, layers):
        _write(layers[1], "pre-bash-broken.sh", "echo boom >&2; exit 3")
        result = conditions.run_step("pre", _bash("ls"))
        assert result.decision is None
        assert "failed (exit 3: boom)" in result.contexts[0]

    def test_timeout_kills_the_process_group(self, layers, monkeypatch, tmp_path):
        monkeypatch.setattr("hooks.config.CONDITIONS_TIMEOUT_SEC", 0.5)
        pidfile = tmp_path / "child.pid"
        _write(layers[1], "pre-bash-slow.sh", f"sleep 30 & echo $! > {pidfile}; wait")
        result = conditions.run_step("pre", _bash("ls"))
        assert "timed out" in result.contexts[0]
        pid = int(pidfile.read_text())
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().split()[2]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"child {pid} survived the timeout")

    def test_executable_without_known_extension(self, layers):
        _write(layers[1], "pre-bash-exe.run", "#!/bin/sh\necho via shebang\n", mode=0o755)
        _write(layers[1], "pre-bash-plain.run", "echo never")
        contexts = conditions.run_step("pre", _bash("ls")).contexts
        assert "[condition pre-bash-exe.run]\nvia shebang" in contexts
        assert any("pre-bash-plain.run] failed (no runner" in c for c in contexts)

    def test_disabled_runs_nothing(self, layers, monkeypatch):
        _write(layers[1], "pre-bash-say.sh", "echo hi")
        monkeypatch.setattr("hooks.config.CONDITIONS_ENABLED", False)
        assert conditions.run_step("pre", _bash("ls")) is None


class TestMerge:
    def test_input_patches_merge_in_layer_order(self, layers, monkeypatch):
        _, global_dir, profile_dir = layers
        _write(global_dir, "pre-bash-a.sh", 'echo \'{"tool_input": {"command": "A", "x": 1}}\'')
        _write(profile_dir, "pre-bash-b.sh", 'echo \'{"tool_input": {"command": "B"}}\'')
        result = conditions.run_step("pre", _bash("ls"))
        assert result.input_patch == {"command": "B", "x": 1}
        assert result.input_writers == ["pre-bash-a.sh", "pre-bash-b.sh"]

    @pytest.mark.parametrize(
        "decisions, expected",
        [(["allow", "ask"], "ask"), (["allow", "deny", "ask"], "deny"), (["allow"], "allow")],
    )
    def test_decision_precedence(self, layers, decisions, expected):
        for i, decision in enumerate(decisions):
            _write(layers[1], f"pre-bash-d{i}.sh", f'echo \'{{"decision": "{decision}"}}\'')
        assert conditions.run_step("pre", _bash("ls")).decision == expected

    def test_bash_output_string_is_shaped(self, layers):
        _write(layers[1], "post-bash-trim.sh", 'echo \'{"tool_output": "short"}\'')
        payload = {
            **_bash("ls"),
            "hook_event_name": "PostToolUse",
            "tool_response": {"stdout": "long", "interrupted": False},
        }
        result = conditions.run_step("post", payload)
        assert result.output == {"stdout": "short", "stderr": "", "interrupted": False}

    def test_string_output_rejected_for_other_builtins(self, layers):
        _write(layers[1], "post-read-trim.sh", 'echo \'{"tool_output": "short"}\'')
        result = conditions.run_step("post", {"tool_name": "Read", "tool_input": {}, "tool_response": {}})
        assert result.output_writers == []
        assert "tool_output must be an object" in result.contexts[0]

    def test_async_is_detached(self, layers, monkeypatch):
        calls = []
        monkeypatch.setattr("hooks._async.fork_and_call", lambda func, *a, **k: calls.append(k["task_name"]))
        _write(layers[1], "pre-bash-ship.async.sh", "echo ignored")
        assert conditions.run_step("pre", _bash("ls")) is None
        assert calls == ["condition:pre-bash-ship.async.sh"]


def _single_json(out: str) -> dict:
    out = out.strip()
    assert out.startswith("{") and out.endswith("}") and "\n" not in out, out
    return json.loads(out)


@pytest.fixture
def isolated_hook(layers, monkeypatch, tmp_path):
    for name in ("_counter_path", "_delivery_path", "_store_path"):
        monkeypatch.setattr(f"hooks.context.enforcement.{name}", lambda n=name: tmp_path / f"{n}.json")
    for flag in ("BRAIN_ENABLED", "BROADCAST_ENABLED", "QUOTA_USAGE_INJECTION_ENABLED"):
        monkeypatch.setattr(f"hooks.config.{flag}", False)
    return layers


@pytest.fixture
def forced_claude(isolated_hook, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    monkeypatch.setattr(emitter, "_forced", True)
    return isolated_hook


class TestPreToolUseEnvelope:
    def test_bypass_rewrite_rides_one_envelope(self, forced_claude, capsys):
        _write(
            forced_claude[1],
            "pre-bash.ls-long.sh",
            'echo \'{"tool_input": {"command": "ls -la"}, "context": "use long"}\'',
        )
        hm.on_pre_tool_use(_bash("ls"))
        out = _single_json(capsys.readouterr().out)["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert out["updatedInput"] == {"command": "ls -la", "description": "probe"}
        assert "use long" in out["additionalContext"]
        assert "tool input rewritten by pre-bash.ls-long.sh" in out["additionalContext"]

    def test_default_mode_drops_rewrite_without_decision(self, forced_claude, capsys):
        _write(forced_claude[1], "pre-bash-long.sh", 'echo \'{"tool_input": {"command": "ls -la"}}\'')
        hm.on_pre_tool_use(_bash("ls", mode="default"))
        out = _single_json(capsys.readouterr().out)["hookSpecificOutput"]
        assert "updatedInput" not in out and "permissionDecision" not in out
        assert "outside bypassPermissions" in out["additionalContext"]

    def test_default_mode_explicit_ask_applies_rewrite(self, forced_claude, capsys):
        _write(
            forced_claude[1], "pre-bash-long.sh", 'echo \'{"tool_input": {"command": "ls -la"}, "decision": "ask"}\''
        )
        hm.on_pre_tool_use(_bash("ls", mode="default"))
        out = _single_json(capsys.readouterr().out)["hookSpecificOutput"]
        assert out["permissionDecision"] == "ask"
        assert out["updatedInput"]["command"] == "ls -la"

    def test_explicit_allow_without_rewrite_is_emitted(self, forced_claude, capsys):
        _write(forced_claude[1], "pre-bash-ok.sh", 'echo \'{"decision": "allow", "reason": "known safe"}\'')
        hm.on_pre_tool_use(_bash("ls", mode="default"))
        out = _single_json(capsys.readouterr().out)["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert "updatedInput" not in out
        assert "known safe" in out["permissionDecisionReason"]

    def test_guards_judge_the_rewritten_input(self, forced_claude, monkeypatch):
        import hooks.context.branch_guard as branch_guard

        seen = []
        monkeypatch.setattr(
            branch_guard,
            "check_branch_guard",
            lambda payload, *a, **k: seen.append(payload["tool_input"]["command"]),
            raising=False,
        )
        _write(forced_claude[1], "pre-bash-swap.sh", 'echo \'{"tool_input": {"command": "git push origin main"}}\'')
        try:
            hm.on_pre_tool_use(_bash("ls"))
        except BlockAction:
            pass
        assert seen == ["git push origin main"]

    def test_deny_blocks_and_carries_context(self, forced_claude):
        _write(forced_claude[1], "pre-bash-note.sh", "echo read the runbook first")
        _write(forced_claude[1], "pre-bash.rm-no.sh", "echo 'rm is fenced' >&2; exit 2")
        with pytest.raises(BlockAction, match="rm is fenced"):
            hm.on_pre_tool_use(_bash("rm -rf build"))
        assert "read the runbook first" in emitter.drain()

    def test_codex_ignores_rewrite_and_turns_ask_into_deny(self, isolated_hook, monkeypatch, capsys):
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
        _state(isolated_hook[0], target="codex")
        _write(isolated_hook[1], "pre-bash-long.sh", 'echo \'{"tool_input": {"command": "ls -la"}}\'')
        payload = _bash("ls")
        hm.on_pre_tool_use(payload)
        assert payload["tool_input"]["command"] == "ls"
        assert "cannot apply it" in emitter.drain()
        _write(isolated_hook[1], "pre-bash-confirm.sh", 'echo \'{"decision": "ask"}\'')
        with pytest.raises(BlockAction, match="cannot prompt"):
            hm.on_pre_tool_use(_bash("ls"))


class TestPostToolUseEnvelope:
    def _post(self, **extra):
        return {
            **_bash("ls"),
            "hook_event_name": "PostToolUse",
            "tool_response": {"stdout": "a\nb\nc", "stderr": "", "interrupted": False, "isImage": False},
            **extra,
        }

    def test_claude_rewrite_and_block_in_one_envelope(self, forced_claude, capsys):
        _write(forced_claude[1], "post-bash-trim.sh", 'echo \'{"tool_output": "a", "context": "trimmed"}\'')
        _write(forced_claude[1], "post-bash-stop.sh", "echo 'secret-looking output' >&2; exit 2")
        hm.on_post_tool_use(self._post())
        emitter.flush("PostToolUse")
        out = _single_json(capsys.readouterr().out)
        assert out["decision"] == "block" and "secret-looking output" in out["reason"]
        assert out["hookSpecificOutput"]["updatedToolOutput"]["stdout"] == "a"
        assert "trimmed" in out["hookSpecificOutput"]["additionalContext"]

    def test_copilot_gets_top_level_context_only(self, isolated_hook, monkeypatch, capsys):
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "copilot")
        _state(isolated_hook[0], target="copilot")
        _write(isolated_hook[1], "post-bash-trim.sh", 'echo \'{"tool_output": "a", "context": "trimmed"}\'')
        hm.on_post_tool_use(self._post())
        emitter.flush("PostToolUse")
        out = _single_json(capsys.readouterr().out)
        assert set(out) == {"additionalContext"}
        assert "trimmed" in out["additionalContext"] and "cannot apply it" in out["additionalContext"]


class TestHookProcess:
    def test_python_m_hooks_emits_rewrite(self, tmp_path):
        home = tmp_path / "ahome"
        bundle = tmp_path / "bundle"
        conditions_dir = bundle / ".claude" / "conditions"
        conditions_dir.mkdir(parents=True)
        home.mkdir()
        (home / "state.json").write_text(json.dumps({"bundle": {"path": str(bundle)}}))
        _write(conditions_dir, "pre-bash.echo-canary.sh", 'echo \'{"tool_input": {"command": "echo rewritten"}}\'')
        env = {
            **os.environ,
            "AGENTIHOOKS_HOME": str(home),
            "AGENTIHOOKS_TARGET": "claude",
            "AGENTIHOOKS_DISABLE_BYPASS_LOOKUP": "1",
            "CONDITIONS_ENABLED": "true",
            "BRAIN_ENABLED": "false",
            "BROADCAST_ENABLED": "false",
        }
        proc = subprocess.run(
            [sys.executable, "-m", "hooks"],
            input=json.dumps(_bash("echo original", cwd=str(tmp_path))),
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
            env=env,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout.strip().splitlines()[-1])["hookSpecificOutput"]
        assert out["updatedInput"]["command"] == "echo rewritten"
        assert out["permissionDecision"] == "allow"
