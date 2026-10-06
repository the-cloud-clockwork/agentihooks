import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import select_profile
from scripts.profiles import measure

CLAUDE_STREAM = "\n".join(
    json.dumps(event)
    for event in (
        {"type": "system", "subtype": "init"},
        {
            "type": "assistant",
            "message": {"usage": {"input_tokens": 2, "cache_creation_input_tokens": 50, "cache_read_input_tokens": 8}},
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
    codex = tmp_path / "codex"
    codex.mkdir()
    (codex / "config.toml").write_text('[mcp_servers.serena]\nurl = "x"\n[mcp_servers.gateway-tools]\nurl = "y"\n')
    monkeypatch.setattr(select_profile.profiles, "_chain", lambda name: [(name, root)])
    monkeypatch.setattr(select_profile.profiles, "render", Mock())
    monkeypatch.setattr(select_profile.profiles, "rendered_root", lambda: tmp_path / "rendered")
    monkeypatch.setattr(select_profile.profiles, "channels", lambda name: "brain")
    monkeypatch.setattr(measure, "codex_home", lambda: codex)
    return home


def _runner(stdout):
    seen = {}

    def run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
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
    assert measure.measure("engineer", "claude", frozenset(), environ=environ, run=run) == 60
    assert seen["argv"][:2] == ["agentihooks", "claude"]
    assert seen["argv"][-7:] == [
        "-p",
        measure.PROMPT,
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        "1",
    ]
    env = seen["env"]
    assert env["KEEP"] == "1" and env["AGENTIHOOKS_PROFILE"] == "engineer"
    assert not {"AGENTIHOOKS_SWARM", "AGENTIHOOKS_SWARM_TASK", "AGENTIHOOKS_AGENT_NAME"} & set(env)
    assert Path(env["CLAUDE_CONFIG_DIR"]) != rendered
    assert seen["cwd"] != str(rendered) and seen["stdin"] is not None
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


def test_codex_first_turn_and_layer_overrides(rendered):
    run, seen = _runner(CODEX_STREAM)
    off = frozenset({"plugins", "mcp", "persona", "hooks"})
    assert measure.measure("engineer", "codex", off, environ={"CLAUDE_CONFIG_DIR": "/x"}, run=run) == 81794
    argv = seen["argv"]
    assert argv[:4] == ["agentihooks", "codex", "-p", "engineer"]
    assert argv[-4:] == ["exec", "--json", "--skip-git-repo-check", measure.PROMPT]
    pairs = {argv[i + 1] for i, a in enumerate(argv) if a == "-c"}
    assert {
        "features.plugins=false",
        "features.hooks=false",
        'developer_instructions=""',
        "mcp_servers.serena.enabled=false",
        "mcp_servers.gateway-tools.enabled=false",
    } <= pairs
    assert "CLAUDE_CONFIG_DIR" not in seen["env"]


def test_codex_full_run_has_no_overrides(rendered):
    run, seen = _runner(CODEX_STREAM)
    measure.measure("engineer", "codex", frozenset(), environ={}, run=run)
    argv = seen["argv"]
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "-c"] == [argv[argv.index("-c") + 1]]
    assert argv[argv.index("-c") + 1].startswith("model_reasoning_effort=")


def test_missing_usage_names_the_failure(rendered):
    def run(argv, **kwargs):
        return Mock(returncode=1, stdout="not json\n", stderr="login required\n")

    with pytest.raises(measure.MeasureError, match="login required"):
        measure.measure("engineer", "claude", frozenset(), environ={}, run=run)


def test_breakdown_runs_full_then_each_layer_off(capsys, monkeypatch):
    tokens = {
        frozenset(): 1000,
        frozenset({"plugins"}): 700,
        frozenset({"mcp"}): 900,
        frozenset({"persona"}): 600,
        frozenset({"hooks"}): 950,
    }
    calls = []

    def fake(name, agent, off, **kwargs):
        calls.append(off)
        return tokens[off]

    monkeypatch.setattr(measure, "measure", fake)
    assert select_profile.dispatch(["profiles", "measure", "engineer", "--breakdown"]) == 0
    assert calls == [frozenset(), *(frozenset({layer}) for layer in measure.LAYERS)]
    assert capsys.readouterr().out.split("\n") == [
        "engineer (claude) first turn input tokens",
        "layer    tokens  cost",
        "full       1000",
        "plugins     700   300",
        "mcp         900   100",
        "persona     600   400",
        "hooks       950    50",
        "",
    ]


def test_single_run_with_layers_off(capsys, monkeypatch):
    seen = []
    monkeypatch.setattr(measure, "measure", lambda name, agent, off, **kw: seen.append((name, agent, off)) or 42)
    assert select_profile.dispatch(["profile", "measure", "master", "--agent", "codex", "--without", "mcp"]) == 0
    assert seen == [("master", "codex", frozenset({"mcp"}))]
    assert capsys.readouterr().out == "master (codex) without mcp: 42 first turn input tokens\n"


def test_failure_exits_nonzero(capsys, monkeypatch):
    def boom(*args, **kwargs):
        raise measure.MeasureError("no usage in session output: login required")

    monkeypatch.setattr(measure, "measure", boom)
    assert select_profile.dispatch(["profiles", "measure", "engineer"]) == 1
    assert capsys.readouterr().err == "ERROR: no usage in session output: login required\n"
