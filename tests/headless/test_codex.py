import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from scripts import codex_router
from scripts.headless import codex
from scripts.headless.request import Request

pytestmark = pytest.mark.unit

SCHEMA = {"type": "object"}


def test_the_read_only_arguments_keep_the_shell_inside_the_read_only_sandbox():
    request = Request("read-only", 'PRO "FILE"', "INPUT", SCHEMA, "gpt-6.1-sol", "high")
    assert codex.arguments(request, Path("/w")) == [
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--output-schema",
        "/w/schema.json",
        "-o",
        "/w/result.json",
        "--disable",
        "apps",
        "--disable",
        "plugins",
        "--disable",
        "multi_agent",
        "--disable",
        "hooks",
        "-c",
        'model_reasoning_effort="high"',
        "-c",
        'developer_instructions="PRO \\"FILE\\""',
        "-m",
        "gpt-6.1-sol",
        "--",
        "INPUT",
    ]


def test_the_sealed_catalog_strips_every_tool_offer():
    listing = "preface\n" + json.dumps(
        {
            "models": [
                {
                    "slug": "m",
                    "tool_mode": "code_mode_only",
                    "apply_patch_tool_type": "freeform",
                    "web_search_tool_type": "x",
                    "multi_agent_version": 2,
                    "experimental_supported_tools": ["a"],
                    "supports_search_tool": True,
                },
                {"slug": "n"},
            ],
            "etag": "e",
        }
    )
    assert codex.sealed_catalog(listing) == {
        "models": [
            {"slug": "m", "experimental_supported_tools": [], "supports_search_tool": False},
            {"slug": "n", "experimental_supported_tools": [], "supports_search_tool": False},
        ],
        "etag": "e",
    }


def test_route_keeps_only_home_path_and_the_routed_token(monkeypatch):
    seen = []
    account = codex_router.CodexAccount("b", env_name="AH_CX_TOKEN_b")

    def fake_route(environ, route, run, exclude):
        seen.append((environ, route, run, exclude))
        return account, "placed", 0, "3"

    monkeypatch.setattr(codex_router, "_route", fake_route)
    monkeypatch.setattr(codex_router, "credential", lambda found, environ: found)
    environ = {"HOME": "/h", "PATH": "/p", "AH_CX_TOKEN_a": "token-a", "AH_CX_TOKEN_b": "token-b", "OTHER": "x"}
    assert codex.route(environ) == (
        account,
        {"HOME": "/h", "PATH": "/p", "AH_CX_TOKEN_b": "token-b", codex_router.TOKEN_ENV: "token-b"},
    )
    assert seen == [(environ, "", codex.subprocess.run, "")]


def test_route_resolves_the_credential_kind(monkeypatch):
    account = codex_router.CodexAccount("b", env_name="AH_CX_TOKEN_b")
    bearer = codex_router.CodexAccount("b", env_name="AH_CX_TOKEN_b", bearer=True)
    monkeypatch.setattr(codex_router, "_route", lambda *_, **__: (account, "placed", 0, "3"))
    monkeypatch.setattr(codex_router, "credential", lambda found, environ: bearer if environ["MARK"] else found)
    monkeypatch.setattr(codex_router, "_chatgpt_account_id", lambda claims: "acct-id")
    monkeypatch.setattr(codex_router, "_claims", lambda token: {})
    routed, child = codex.route({"AH_CX_TOKEN_b": "token-b", "MARK": "1"})
    assert routed is bearer
    assert child == {
        "AH_CX_TOKEN_b": "token-b",
        codex_router.TOKEN_ENV: "token-b",
        codex_router.ACCOUNT_ID_ENV: "acct-id",
    }


def test_route_seeds_an_api_key(monkeypatch):
    account = codex_router.CodexAccount("api", key_env="OPENAI_API_KEY")
    seen = {}

    def child_environment(found, seed):
        seen["seed"] = seed
        return dict(seed)

    monkeypatch.setattr(codex_router, "_route", lambda *_, **__: (account, "forced", 0, "?"))
    monkeypatch.setattr(codex_router, "credential", lambda found, environ: found)
    monkeypatch.setattr(codex_router, "child_environment", child_environment)
    codex.route({"OPENAI_API_KEY": "key", "PATH": "/p", "OTHER": "x"})
    assert seen["seed"] == {"PATH": "/p", "OPENAI_API_KEY": "key"}


def test_config_values_keep_characters_outside_the_basic_plane_valid_toml():
    request = Request("sealed", "PRO \U0001f600", "INPUT", SCHEMA, "m", "l\u00f6w")
    args = codex.arguments(request, Path("/w\u00e9"))
    values = [args[at + 1] for at, arg in enumerate(args) if arg == "-c"]
    assert values[2:] == [
        'model_catalog_json="/w\u00e9/catalog-sealed.json"',
        'model_reasoning_effort="l\u00f6w"',
        'developer_instructions="PRO \U0001f600"',
    ]
    assert [tomllib.loads(value) for value in values[2:]] == [
        {"model_catalog_json": "/w\u00e9/catalog-sealed.json"},
        {"model_reasoning_effort": "l\u00f6w"},
        {"developer_instructions": "PRO \U0001f600"},
    ]


def test_the_sealed_catalog_ignores_text_after_the_listing():
    assert codex.sealed_catalog('{"models": []}\ndone') == {"models": []}


def test_a_run_that_writes_no_result_file_replies_empty(monkeypatch, tmp_path):
    account = codex_router.CodexAccount("b", env_name="AH_CX_TOKEN_b")
    monkeypatch.setattr(
        codex, "execute", lambda command, env: subprocess.CompletedProcess(command, 0, '{"models": []}', "")
    )
    request = Request("sealed", "P", "I", SCHEMA, "m", "low")
    assert codex.invoke(request, (account, {}), tmp_path) == ""
