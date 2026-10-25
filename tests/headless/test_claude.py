import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hooks.context import account_sessions
from scripts import claude_quota_balancer, operator_env
from scripts.headless import claude
from scripts.headless.request import Request

pytestmark = pytest.mark.unit

SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}


def test_the_read_only_command_offers_only_read_search_and_web_tools():
    command = claude.command(Request("read-only", "PROFILE", "INPUT", SCHEMA, "opus", "high"))
    assert command == [
        "claude",
        "-p",
        "--tools",
        "Read,Grep,Glob,WebSearch,WebFetch",
        "--permission-mode",
        "dontAsk",
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
        "opus",
        "--effort",
        "high",
        "--",
        "INPUT",
    ]


def test_route_keeps_only_home_path_and_the_routed_token(monkeypatch):
    seen = {}

    def select(pool, **kwargs):
        seen.update(kwargs, pool=pool)
        return SimpleNamespace(credential=claude_quota_balancer.Credential("AH_CC_TOKEN_b", "token-b"))

    monkeypatch.setattr(claude_quota_balancer, "select_credential", select)
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda: {"b": 1})
    monkeypatch.setattr(operator_env, "accounts", lambda environ: {"AH_CC_TOKEN_c": environ["OTHER"]})
    environ = {"HOME": "/h", "PATH": "/p", "AH_CC_TOKEN_a": "token-a", "OTHER": "x"}
    assert claude.route(environ) == {
        "HOME": "/h",
        "PATH": "/p",
        "AH_CC_TOKEN_b": "token-b",
        "CLAUDE_CODE_OAUTH_TOKEN": "token-b",
    }
    assert seen == {
        "pool": {**environ, "AH_CC_TOKEN_c": "x"},
        "sessions": {"b": 1},
        "exclude": [account_sessions.API_ACCOUNT],
    }


def test_route_skips_home_and_path_when_absent(monkeypatch):
    credential = claude_quota_balancer.Credential("AH_CC_TOKEN_b", "token-b")
    monkeypatch.setattr(
        claude_quota_balancer, "select_credential", lambda pool, **_: SimpleNamespace(credential=credential)
    )
    monkeypatch.setattr(account_sessions, "sessions_by_account", dict)
    monkeypatch.setattr(operator_env, "accounts", lambda environ: {})
    assert claude.route({}) == {"AH_CC_TOKEN_b": "token-b", "CLAUDE_CODE_OAUTH_TOKEN": "token-b"}


def test_reply_prefers_the_structured_output_of_the_last_result_event():
    stdout = "\n".join(
        [
            json.dumps({"type": "result", "structured_output": {"verdict": "old"}}),
            "not json",
            json.dumps(["list"]),
            json.dumps({"type": "result", "result": "{}", "structured_output": {"verdict": "pass"}}),
            json.dumps({"type": "system"}),
            "trailing noise",
        ]
    )
    assert claude.reply(stdout) == {"verdict": "pass"}


def test_invoke_runs_the_command_in_the_routed_environment(monkeypatch):
    seen = []

    def execute(command, env):
        seen.append((command, env))
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"type": "result", "structured_output": {"v": 1}}), ""
        )

    monkeypatch.setattr(claude, "execute", execute)
    request = Request("sealed", "P", "I", SCHEMA, "sonnet", "low")
    assert claude.invoke(request, {"PATH": "/p"}, Path("/w")) == {"v": 1}
    assert seen == [(claude.command(request), {"PATH": "/p"})]


def test_reply_falls_back_to_the_result_text():
    assert claude.reply(json.dumps({"type": "result", "result": '{"verdict": "x"}', "structured_output": None})) == (
        '{"verdict": "x"}'
    )


def test_reply_without_a_result_text_is_empty():
    assert claude.reply(json.dumps({"type": "result"})) == ""


def test_reply_without_a_result_event_is_empty():
    assert claude.reply(json.dumps({"type": "assistant"})) == ""
