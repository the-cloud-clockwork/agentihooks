import json
from pathlib import Path

import jsonschema
import pytest

from scripts import codex_router
from scripts.headless import claude, codex, runner
from scripts.headless.request import HeadlessError

pytestmark = pytest.mark.unit

SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}
LISTING = json.dumps({"models": [{"slug": "gpt-6-luna", "tool_mode": "code_mode_only"}]})
CODEX_SEALED_FEATURES = [
    "shell_tool",
    "unified_exec",
    "code_mode",
    "code_mode_host",
    "apps",
    "plugins",
    "multi_agent",
    "multi_agent_v2",
    "goals",
    "sleep_tool",
    "view_image",
    "image_generation",
    "browser_use",
    "computer_use",
    "skill_search",
    "tool_suggest",
    "hooks",
]


def result_event(verdict: str) -> str:
    return json.dumps({"type": "result", "result": "", "structured_output": {"verdict": verdict}})


@pytest.fixture
def routed(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
    for name in ("AGENTIHOOKS_TIER_HIGH_CLAUDE", "AGENTIHOOKS_TIER_LOW_CLAUDE"):
        monkeypatch.delenv(name, raising=False)
    for name in ("AGENTIHOOKS_TIER_HIGH_CODEX", "AGENTIHOOKS_TIER_LOW_CODEX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(claude, "route", lambda environ: {"PATH": environ["PATH"]})
    account = codex_router.CodexAccount("acct", env_name="AH_CX_TOKEN_acct")
    monkeypatch.setattr(codex, "route", lambda environ: (account, {"PATH": environ["PATH"]}))


def test_a_sealed_claude_run_invokes_exactly_the_sealed_flag_set(fake_cli, routed):
    calls = fake_cli("claude", [json.dumps({"type": "system"}) + "\n" + result_event("pass")])
    assert runner.run("PROFILE", "INPUT", SCHEMA, "sealed", "low", "low", harness="claude") == {"verdict": "pass"}
    assert calls() == [
        [
            "-p",
            "--tools",
            "",
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--settings",
            '{"disableAllHooks":true}',
            "--no-session-persistence",
            "--disable-slash-commands",
            "--system-prompt",
            "PROFILE",
            "--json-schema",
            json.dumps(SCHEMA),
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            "sonnet",
            "--effort",
            "low",
            "--",
            "INPUT",
        ]
    ]


def test_a_sealed_codex_run_invokes_exactly_the_sealed_flag_set(fake_cli, routed):
    calls = fake_cli("codex", ['{"verdict": "pass"}'], listing="catalog follows\n" + LISTING)
    assert runner.run("PROFILE", "INPUT", SCHEMA, "sealed", "low", "low", harness="codex") == {"verdict": "pass"}
    listing_call, exec_call = calls()
    assert listing_call == ["--no-daemon", "debug", "models"]
    work = exec_call[exec_call.index("-o") + 1].removesuffix("/result.json")
    disabled = [part for feature in CODEX_SEALED_FEATURES for part in ("--disable", feature)]
    assert exec_call == [
        "--no-daemon",
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--output-schema",
        f"{work}/schema.json",
        "-o",
        f"{work}/result.json",
        *disabled,
        "-c",
        'web_search="disabled"',
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        f'model_catalog_json="{work}/catalog-sealed.json"',
        "-c",
        'model_reasoning_effort="low"',
        "-c",
        'developer_instructions="PROFILE"',
        "-m",
        "gpt-6-luna",
        "--",
        "INPUT",
    ]


def test_codex_gets_its_own_empty_home_the_schema_and_the_sealed_catalog(fake_cli, routed, monkeypatch):
    fake_cli("codex", ['{"verdict": "pass"}'], listing=LISTING)
    seen = []

    real_execute = codex.execute

    def execute(command, env):
        work = Path(env["CODEX_HOME"]).parent
        seen.append(
            {
                "env": sorted(env),
                "home": sorted(p.name for p in work.iterdir()) if command[-1] == "INPUT" else None,
                "schema": json.loads((work / "schema.json").read_text()),
                "catalog": json.loads((work / "catalog-sealed.json").read_text()) if command[-1] == "INPUT" else None,
            }
        )
        return real_execute(command, env)

    monkeypatch.setattr(codex, "execute", execute)
    runner.run("PROFILE", "INPUT", SCHEMA, "sealed", "high", "low", harness="codex")
    assert seen[1] == {
        "env": ["CODEX_HOME", "PATH"],
        "home": ["catalog-sealed.json", "codex-home", "schema.json"],
        "schema": SCHEMA,
        "catalog": {
            "models": [{"slug": "gpt-6-luna", "experimental_supported_tools": [], "supports_search_tool": False}]
        },
    }


def test_a_read_only_codex_run_builds_no_sealed_catalog(fake_cli, routed):
    calls = fake_cli("codex", ['{"verdict": "pass"}'])
    runner.run("PROFILE", "INPUT", SCHEMA, "read-only", "high", "high", harness="codex")
    (only,) = calls()
    assert only[only.index("-m") + 1] == "gpt-6.1-sol"


def test_a_non_json_reply_is_retried_once_then_succeeds(fake_cli, routed):
    calls = fake_cli("claude", ["not json", result_event("retry")])
    assert runner.run("P", "I", SCHEMA, "sealed", "low", "low", harness="claude") == {"verdict": "retry"}
    assert len(calls()) == 2


def test_a_non_json_reply_is_retried_once_then_raises(fake_cli, routed):
    calls = fake_cli("codex", ["not json"], listing=LISTING)
    with pytest.raises(HeadlessError) as caught:
        runner.run("P", "I", SCHEMA, "sealed", "low", "low", harness="codex")
    assert str(caught.value) == (
        "codex returned no reply matching the schema after 2 attempts: Expecting value: line 1 column 1 (char 0)"
    )
    assert [call[1] for call in calls()] == ["debug", "exec", "debug", "exec"]


def test_a_reply_off_the_schema_is_retried_then_raises(fake_cli, routed):
    calls = fake_cli("claude", [json.dumps({"type": "result", "structured_output": {"other": 1}})])
    with pytest.raises(HeadlessError) as caught:
        runner.run("P", "I", SCHEMA, "sealed", "low", "low", harness="claude")
    with pytest.raises(jsonschema.ValidationError) as invalid:
        jsonschema.validate({"other": 1}, SCHEMA)
    assert str(caught.value) == f"claude returned no reply matching the schema after 2 attempts: {invalid.value}"
    assert len(calls()) == 2


@pytest.mark.parametrize(
    ("harness", "tier", "flag", "model"),
    [
        ("claude", "low", "--model", "sonnet"),
        ("claude", "high", "--model", "opus"),
        ("codex", "low", "-m", "gpt-6-luna"),
    ],
)
def test_the_tier_maps_to_the_model_of_each_harness(fake_cli, routed, harness, tier, flag, model):
    reply = result_event("pass") if harness == "claude" else '{"verdict": "pass"}'
    calls = fake_cli(harness, [reply], listing=LISTING)
    runner.run("P", "I", SCHEMA, "read-only", tier, "low", harness=harness)
    (call,) = calls()
    assert call[call.index(flag) + 1] == model


def test_auto_uses_the_callers_harness(fake_cli, routed, monkeypatch):
    calls = fake_cli("codex", ['{"verdict": "pass"}'])
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    runner.run("P", "I", SCHEMA, "read-only", "low", "low")
    assert len(calls()) == 1


@pytest.mark.parametrize("target", ["", None])
def test_auto_defaults_to_claude(target):
    environ = {} if target is None else {"AGENTIHOOKS_TARGET": target}
    assert runner.harness_for("auto", environ) == "claude"


def test_an_explicit_harness_wins_over_the_target():
    assert runner.harness_for("claude", {"AGENTIHOOKS_TARGET": "codex"}) == "claude"


def test_a_harness_without_an_adapter_is_refused():
    with pytest.raises(ValueError) as caught:
        runner.harness_for("auto", {"AGENTIHOOKS_TARGET": "copilot"})
    assert str(caught.value) == "no headless adapter for copilot"


def test_an_unknown_clamp_is_refused(routed):
    with pytest.raises(ValueError) as caught:
        runner.run("P", "I", SCHEMA, "open", "low", "low", harness="claude")
    assert str(caught.value) == "clamp must be one of sealed, read-only"


def test_validated_accepts_a_parsed_reply():
    assert runner.validated({"verdict": "x"}, SCHEMA) == {"verdict": "x"}
