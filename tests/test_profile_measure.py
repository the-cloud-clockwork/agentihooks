import json
import os
import subprocess
import tomllib
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import select_profile
from scripts.profiles import measure, render

CLAUDE_STREAM = "\n".join(
    json.dumps(event)
    for event in (
        42,
        {
            "type": "system",
            "subtype": "init",
            "mcp_servers": [{"name": "serena", "status": "connected"}, {"name": "gateway-tools", "status": "pending"}],
        },
        {
            "type": "assistant",
            "message": {"usage": {"input_tokens": 2, "cache_creation_input_tokens": 50}},
        },
        {
            "type": "assistant",
            "message": {"usage": {"input_tokens": 900, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}},
        },
        {"type": "result", "usage": {"input_tokens": 999}},
    )
)
CODEX_STREAM = "\n".join(
    [
        "[agentihooks codex] account=default",
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 81794, "cached_input_tokens": 12288}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}),
    ]
)


@pytest.fixture
def rendered(monkeypatch, tmp_path):
    root = tmp_path / "profile"
    root.mkdir()
    home = tmp_path / "rendered" / "engineer" / "claude"
    (home / "rules").mkdir(parents=True)
    (home / "rules" / "a.md").write_text("rule")
    (home / "skills").mkdir()
    (home / "CLAUDE.md").write_text("persona")
    (home / "settings.json").write_text(
        json.dumps({"env": {"X": "1"}, "hooks": {"SessionStart": []}, "enabledPlugins": {"a@m": True, "b@m": False}})
    )
    (home / ".claude.json").write_text(json.dumps({"userID": "u", "mcpServers": {"serena": {"url": "x"}}}))
    codex = tmp_path / "rendered" / "engineer" / "codex"
    (codex / "skills").mkdir(parents=True)
    (codex / "AGENTS.md").symlink_to(home / "CLAUDE.md")
    (codex / "hooks.json").symlink_to(tmp_path / "operator-hooks.json")
    config = '[features]\nhooks = true\n[mcp_servers.serena]\nurl = "x"\n[mcp_servers.gateway-tools]\nurl = "y"\n'
    config += f'[hooks.state."{codex}/hooks.json:stop:0:0"]\ntrusted_hash = "sha256:aa"\n'
    (codex / "config.toml").write_text(config)
    monkeypatch.setattr(select_profile.profiles, "_chain", lambda name: [(name, root)])
    monkeypatch.setattr(select_profile.profiles, "render", Mock())
    monkeypatch.setattr(select_profile.profiles, "rendered_root", lambda: tmp_path / "rendered")
    monkeypatch.setattr(select_profile.profiles, "channels", lambda name: "brain")
    return home


def _runner(stdout):
    seen = {}

    def run(argv, **kwargs):
        seen.update(argv=argv, work=os.listdir(kwargs["cwd"]), **kwargs)
        codex = kwargs["env"].get("CODEX_HOME")
        if codex:
            seen["home"] = {p.name: Path(os.readlink(p)) if p.is_symlink() else None for p in Path(codex).iterdir()}
            seen["config"] = tomllib.loads((Path(codex) / "config.toml").read_text())
        home = kwargs["env"].get("CLAUDE_CONFIG_DIR")
        if home:
            seen["home"] = {p.name: Path(os.readlink(p)) if p.is_symlink() else None for p in Path(home).iterdir()}
            seen["settings"] = json.loads((Path(home) / "settings.json").read_text())
            seen["claude_json"] = json.loads((Path(home) / ".claude.json").read_text())
        return Mock(returncode=0, stdout=stdout, stderr="")

    return run, seen


def test_claude_first_turn_is_the_first_request_input(rendered):
    run, seen = _runner(CLAUDE_STREAM)
    environ = {"AGENTIHOOKS_SWARM": "s", "AGENTIHOOKS_SWARM_TASK": "t", "AGENTIHOOKS_AGENT_NAME": "a", "KEEP": "1"}
    assert measure.measure("engineer", "claude", frozenset(), environ=environ, run=run) == measure.Reading(
        52, ("gateway-tools",)
    )
    _, flags = select_profile.prepare("engineer", "claude", "", "", measure.CLAUDE_ARGS, {})
    assert seen["argv"] == ["agentihooks", "claude", *flags]
    assert flags[-7:] == ["-p", measure.PROMPT, "--output-format", "stream-json", "--verbose", "--max-turns", "1"]
    assert seen["work"] == [] and seen["cwd"] != seen["env"]["CLAUDE_CONFIG_DIR"]
    assert (seen["stdin"], seen["capture_output"], seen["text"], seen["timeout"]) == (
        subprocess.DEVNULL,
        True,
        True,
        measure.TIMEOUT_S,
    )
    env = seen["env"]
    assert env["KEEP"] == "1" and env["AGENTIHOOKS_PROFILE"] == "engineer"
    assert not {"AGENTIHOOKS_SWARM", "AGENTIHOOKS_SWARM_TASK", "AGENTIHOOKS_AGENT_NAME"} & set(env)
    assert Path(env["CLAUDE_CONFIG_DIR"]) != rendered
    assert seen["home"]["CLAUDE.md"] == rendered / "CLAUDE.md"
    assert seen["home"]["rules"] == rendered / "rules"
    assert seen["home"]["settings.json"] is None and seen["home"][".claude.json"] is None
    assert seen["settings"] == json.loads((rendered / "settings.json").read_text())
    assert seen["claude_json"]["mcpServers"] == {"serena": {"url": "x"}}


@pytest.mark.parametrize(
    "layer,check",
    [
        ("plugins", lambda s: s["settings"]["enabledPlugins"] == {"a@m": False, "b@m": False}),
        ("mcp", lambda s: s["claude_json"] == {"userID": "u", "mcpServers": {}}),
        ("persona", lambda s: "CLAUDE.md" not in s["home"] and "rules" not in s["home"] and "skills" in s["home"]),
        ("hooks", lambda s: "hooks" not in s["settings"] and s["settings"]["env"] == {"X": "1"}),
    ],
)
def test_claude_layer_off_changes_only_that_layer(rendered, layer, check):
    run, seen = _runner(CLAUDE_STREAM)
    measure.measure("engineer", "claude", frozenset({layer}), environ={}, run=run)
    assert check(seen)
    assert json.loads((rendered / "settings.json").read_text())["enabledPlugins"]["a@m"] is True
    assert (rendered / "CLAUDE.md").exists()


def test_codex_first_turn_runs_in_a_copy_of_the_profile_codex_home(rendered):
    codex = rendered.parent / "codex"
    run, seen = _runner(CODEX_STREAM)
    environ = {"CLAUDE_CONFIG_DIR": "/x"}
    assert measure.measure("engineer", "codex", frozenset(), environ=environ, run=run) == measure.Reading(81794, ())
    argv = seen["argv"]
    assert argv[:2] == ["agentihooks", "codex"] and "-p" not in argv
    assert argv[-4:] == ["exec", "--json", "--skip-git-repo-check", measure.PROMPT]
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "-c"] == [argv[argv.index("-c") + 1]]
    assert "CLAUDE_CONFIG_DIR" not in seen["env"]
    home = Path(seen["env"]["CODEX_HOME"])
    assert home != codex
    assert seen["home"] == {
        "AGENTS.md": codex / "AGENTS.md",
        "skills": codex / "skills",
        "hooks.json": codex / "hooks.json",
        "config.toml": None,
    }
    assert seen["config"]["hooks"]["state"] == {f"{home}/hooks.json:stop:0:0": {"trusted_hash": "sha256:aa"}}
    assert set(seen["config"]["mcp_servers"]) == {"serena", "gateway-tools"}


@pytest.mark.parametrize(
    "layer,check",
    [
        ("plugins", lambda s: s["config"]["features"] == {"hooks": True, "plugins": False}),
        ("mcp", lambda s: "mcp_servers" not in s["config"]),
        ("persona", lambda s: "AGENTS.md" not in s["home"] and "skills" in s["home"]),
        ("hooks", lambda s: s["config"]["features"] == {"hooks": False}),
    ],
)
def test_codex_layer_off_changes_only_that_layer(rendered, layer, check):
    run, seen = _runner(CODEX_STREAM)
    measure.measure("engineer", "codex", frozenset({layer}), environ={}, run=run)
    assert check(seen)


def test_hooks_off_when_the_profile_has_no_hooks(rendered):
    (rendered / "settings.json").write_text(json.dumps({"env": {}}))
    run, seen = _runner(CLAUDE_STREAM)
    measure.measure("engineer", "claude", frozenset({"hooks"}), environ={}, run=run)
    assert seen["settings"] == {"env": {}}


def test_init_without_servers_and_usage_without_cache_keys():
    stdout = '{"subtype": "init"}\n{"type": "assistant", "message": {"usage": {"input_tokens": 3}}}'
    assert measure.first_turn("claude", stdout) == measure.Reading(3, ())


def test_missing_usage_names_the_failure(rendered):
    stderr = "x" * 600 + "login required\n"

    def run(argv, **kwargs):
        return Mock(returncode=1, stdout="not json\n", stderr=stderr)

    with pytest.raises(measure.MeasureError) as exc:
        measure.measure("engineer", "claude", frozenset(), environ={}, run=run)
    assert str(exc.value) == "no usage in session output: " + stderr.strip()[-500:]


def test_breakdown_runs_full_then_each_layer_off(capsys, monkeypatch):
    tokens = {
        frozenset(): measure.Reading(1000, ("gateway-tools",)),
        frozenset({"plugins"}): measure.Reading(700, ()),
        frozenset({"mcp"}): measure.Reading(900, ()),
        frozenset({"persona"}): measure.Reading(600, ("gateway-tools", "serena")),
        frozenset({"hooks"}): measure.Reading(950, ()),
    }
    calls = []

    def fake(name, agent, off, **kwargs):
        calls.append((name, agent, off))
        return tokens[off]

    monkeypatch.setattr(measure, "measure", fake)
    assert render.main(["measure", "engineer", "--agent", "claude", "--breakdown"]) == 0
    assert calls == [("engineer", "claude", off) for off in [frozenset(), *(frozenset({n}) for n in measure.LAYERS)]]
    assert capsys.readouterr().out.split("\n") == [
        "engineer (claude) first turn input tokens",
        "layer    tokens  cost  mcp not connected",
        "full       1000        gateway-tools",
        "plugins     700   300",
        "mcp         900   100",
        "persona     600   400  gateway-tools,serena",
        "hooks       950    50",
        "",
    ]


@pytest.mark.parametrize(
    "argv,reading,line",
    [
        (
            ["master", "--agent", "codex", "--without", "mcp", "--without", "persona"],
            measure.Reading(42, ("gateway-tools", "serena")),
            "master (codex) without mcp,persona: 42 first turn input tokens, mcp not connected: gateway-tools,serena",
        ),
        (["engineer"], measure.Reading(42, ()), "engineer (claude): 42 first turn input tokens"),
    ],
)
def test_single_run(capsys, monkeypatch, argv, reading, line):
    seen = []
    monkeypatch.setattr(measure, "measure", lambda name, agent, off, **kw: seen.append((name, agent, off)) or reading)
    assert select_profile.dispatch(["profiles", "measure", *argv]) == 0
    assert seen == [(argv[0], "codex" if "codex" in argv else "claude", frozenset(argv[4::2]))]
    assert capsys.readouterr().out == line + "\n"


@pytest.mark.parametrize("argv", [["engineer", "--agent", "copilot"], ["engineer", "--without", "skills"]])
def test_unknown_agent_or_layer_is_refused(argv, monkeypatch):
    monkeypatch.setattr(measure, "measure", Mock())
    with pytest.raises(SystemExit) as exc:
        render.main(["measure", *argv])
    assert exc.value.code == 2
    measure.measure.assert_not_called()


def test_help_lists_measure(capsys):
    with pytest.raises(SystemExit):
        render.main(["--help"])
    assert "  Print a profile's first turn input tokens\n" in capsys.readouterr().out


def test_installer_dispatches_profiles(monkeypatch):
    from scripts import install

    dispatch = Mock(return_value=5)
    monkeypatch.setattr(select_profile, "dispatch", dispatch)
    monkeypatch.setattr(install.sys, "argv", ["agentihooks", "profiles", "measure", "engineer"])
    with pytest.raises(SystemExit) as exc:
        install.main()
    assert exc.value.code == 5
    dispatch.assert_called_once_with(["profiles", "measure", "engineer"])


def test_failure_exits_nonzero(capsys, monkeypatch):
    def boom(*args, **kwargs):
        raise measure.MeasureError("no usage in session output: login required")

    monkeypatch.setattr(measure, "measure", boom)
    assert select_profile.dispatch(["profiles", "measure", "engineer"]) == 1
    assert capsys.readouterr().err == "ERROR: no usage in session output: login required\n"
