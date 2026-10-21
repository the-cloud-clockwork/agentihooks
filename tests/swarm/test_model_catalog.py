import io
import json
import subprocess

import pytest

from scripts.swarm import model_catalog

HELP = """Usage: claude [options] [command] [prompt]

Options:
  --effort <level>                      Effort level for the current session
                                        (low, medium, high, xhigh, max)
  --environment <environment_id>        Create a new cloud session that runs on
                                        the given self-hosted environment
  --model <model>                       Model for the current session. Provide
                                        an alias for the latest model (e.g.
                                        'fable', 'opus', or 'sonnet') or a
                                        model's full name.
  --name <name>                         Name the session
"""


def level(name):
    return {"effort": name, "description": f"{name} reasoning"}


CODEX_MODELS = [
    {
        "slug": "gpt-6-astra",
        "display_name": "GPT-6-Astra",
        "visibility": "list",
        "priority": 2,
        "supported_reasoning_levels": [level("medium"), level("high")],
    },
    {
        "slug": "gpt-reserve",
        "display_name": "GPT-Reserve",
        "visibility": "hide",
        "priority": 0,
        "supported_reasoning_levels": [level("medium")],
    },
    {
        "slug": "gpt-6.1-sol",
        "display_name": "GPT-6.1-Sol",
        "visibility": "list",
        "priority": 1,
        "supported_reasoning_levels": [level("low"), level("medium"), level("xhigh")],
    },
    {"slug": "gpt-plain", "visibility": "list", "priority": 3, "supported_reasoning_levels": []},
]


def effort(*names):
    supported = {name: {"supported": name in names} for name in ("low", "medium", "high", "xhigh", "max")}
    return {"effort": {"supported": True, **supported}}


CLAUDE_MODELS = [
    {
        "id": "claude-opus-5",
        "display_name": "Claude Opus 5",
        "line": "opus",
        "created_at": "2026-07-24T00:00:00Z",
        "capabilities": effort("low", "high"),
    },
    {
        "id": "claude-sonnet-5-5",
        "display_name": "Claude Sonnet 5.5",
        "line": "sonnet",
        "created_at": "2026-09-28T00:00:00Z",
        "capabilities": effort("medium", "xhigh"),
    },
    {
        "id": "claude-opus-5-5",
        "display_name": "Claude Opus 5.5",
        "line": "opus",
        "created_at": "2026-09-21T16:24:00Z",
        "capabilities": effort("low", "medium", "high", "xhigh", "max"),
    },
    {
        "id": "claude-sonnet-4-6",
        "display_name": "Claude Sonnet 4.6",
        "line": "sonnet",
        "created_at": "2026-02-17T00:00:00Z",
        "capabilities": effort("low"),
    },
]


@pytest.fixture
def codex_home(tmp_path):
    (tmp_path / "models_cache.json").write_text(json.dumps({"client_version": "0.162.1", "models": CODEX_MODELS}))
    return {"CODEX_HOME": str(tmp_path)}


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


def test_codex_keeps_only_listed_models_in_picker_order_with_their_levels(codex_home):
    assert model_catalog.codex(codex_home) == [
        {"model": "gpt-6.1-sol", "label": "GPT-6.1-Sol", "efforts": ["low", "medium", "xhigh"]},
        {"model": "gpt-6-astra", "label": "GPT-6-Astra", "efforts": ["medium", "high"]},
        {"model": "gpt-plain", "label": "gpt-plain", "efforts": []},
    ]


@pytest.mark.parametrize("body", [None, "not json", json.dumps({"models": [{"slug": "x", "visibility": "list"}]})])
def test_an_unreadable_codex_cache_gives_no_models(tmp_path, body):
    if body is not None:
        (tmp_path / "models_cache.json").write_text(body)
    assert model_catalog.codex({"CODEX_HOME": str(tmp_path)}) == []


def unreachable(*args):
    raise OSError("unreachable")


def missing_data(*args):
    raise KeyError("data")


def no_claude():
    raise FileNotFoundError("claude")


def slow_claude():
    raise subprocess.TimeoutExpired("claude", 20)


def unread_help():
    pytest.fail("the help is read only as a fallback")


def test_claude_maps_each_line_to_its_newest_model_under_the_cli_alias(monkeypatch):
    seen = []
    monkeypatch.setattr(model_catalog, "fetch_models", lambda token: seen.append(token) or CLAUDE_MODELS)
    monkeypatch.setattr(model_catalog, "cli_help", unread_help)
    assert model_catalog.claude({"AH_CC_TOKEN_one": "routed-credential", "HOME": "/h"}) == [
        {"model": "opus", "label": "Claude Opus 5.5", "efforts": ["low", "medium", "high", "xhigh", "max"]},
        {"model": "sonnet", "label": "Claude Sonnet 5.5", "efforts": ["medium", "xhigh"]},
    ]
    assert seen == ["routed-credential"]


def test_claude_reads_with_the_oauth_credential_when_no_account_is_routed(monkeypatch):
    seen = []
    monkeypatch.setattr(model_catalog, "fetch_models", lambda token: seen.append(token) or CLAUDE_MODELS[:1])
    assert model_catalog.claude({"CLAUDE_CODE_OAUTH_TOKEN": "oauth-credential"}) == [
        {"model": "opus", "label": "Claude Opus 5", "efforts": ["low", "high"]}
    ]
    assert seen == ["oauth-credential"]


@pytest.mark.parametrize(
    "environ,fetch,fetched",
    [
        ({"AH_CC_TOKEN_one": "routed-credential"}, unreachable, ["routed-credential"]),
        ({"AH_CC_TOKEN_one": "routed-credential"}, missing_data, ["routed-credential"]),
        ({"AH_CC_TOKEN_one": "routed-credential"}, lambda token: [], ["routed-credential"]),
        ({"AH_CC_TOKEN_one": "a", "AH_CC_TOKEN_two": "b"}, unreachable, []),
        ({}, unreachable, []),
    ],
)
def test_claude_falls_back_to_the_aliases_the_installed_cli_documents(monkeypatch, environ, fetch, fetched):
    calls = []
    monkeypatch.setattr(model_catalog, "fetch_models", lambda token: calls.append(token) or fetch(token))
    monkeypatch.setattr(model_catalog, "cli_help", lambda: HELP)
    efforts = ["low", "medium", "high", "xhigh", "max"]
    assert model_catalog.claude(environ) == [
        {"model": "fable", "label": "fable", "efforts": efforts},
        {"model": "opus", "label": "opus", "efforts": efforts},
        {"model": "sonnet", "label": "sonnet", "efforts": efforts},
    ]
    assert calls == fetched


@pytest.mark.parametrize(
    "cli_help",
    [no_claude, slow_claude, lambda: "", lambda: HELP.replace("--model <model>", "--modelx")],
)
def test_claude_with_no_readable_source_gives_no_models(monkeypatch, cli_help):
    monkeypatch.setattr(model_catalog, "fetch_models", unreachable)
    monkeypatch.setattr(model_catalog, "cli_help", cli_help)
    assert model_catalog.claude({"AH_CC_TOKEN_one": "routed-credential"}) == []


def test_the_help_without_an_effort_line_lists_aliases_with_no_efforts():
    text = HELP.replace("--effort <level>", "--effortx")
    assert model_catalog.from_help(text) == [
        {"model": "fable", "label": "fable", "efforts": []},
        {"model": "opus", "label": "opus", "efforts": []},
        {"model": "sonnet", "label": "sonnet", "efforts": []},
    ]


def test_the_effort_levels_come_only_from_the_effort_option_itself():
    text = HELP.replace("(low, medium, high, xhigh, max)", "for this session") + "  --print  Print (json, text)\n"
    assert model_catalog.from_help(text) == [
        {"model": "fable", "label": "fable", "efforts": []},
        {"model": "opus", "label": "opus", "efforts": []},
        {"model": "sonnet", "label": "sonnet", "efforts": []},
    ]


def test_the_first_listed_model_wins_a_tie_on_its_line():
    twin = {**CLAUDE_MODELS[2], "id": "claude-opus-5-5-twin", "display_name": "Claude Opus Twin"}
    assert model_catalog.from_api([CLAUDE_MODELS[2], twin]) == [
        {"model": "opus", "label": "Claude Opus 5.5", "efforts": ["low", "medium", "high", "xhigh", "max"]}
    ]


@pytest.mark.parametrize(
    "environ,fetch",
    [({}, unreachable), ({"AH_CC_TOKEN_one": "routed-credential"}, unreachable), ({"OTHER": "x"}, missing_data)],
)
def test_an_unread_account_lists_nothing(monkeypatch, environ, fetch):
    monkeypatch.setattr(model_catalog, "fetch_models", fetch)
    assert model_catalog.from_account(environ) == []


@pytest.mark.parametrize("cli_help", [no_claude, slow_claude])
def test_an_unreadable_help_documents_nothing(monkeypatch, cli_help):
    monkeypatch.setattr(model_catalog, "cli_help", cli_help)
    assert model_catalog.documented() == []


def test_fetch_models_asks_the_anthropic_models_endpoint_with_the_oauth_bearer(monkeypatch):
    sent = {}

    def urlopen(request, timeout):
        sent.update(url=request.full_url, headers=dict(request.header_items()), timeout=timeout)
        return io.BytesIO(json.dumps({"data": CLAUDE_MODELS}).encode())

    monkeypatch.setattr(model_catalog.urllib.request, "urlopen", urlopen)
    assert model_catalog.fetch_models("routed-credential") == CLAUDE_MODELS
    assert sent == {
        "url": "https://api.anthropic.com/v1/models?limit=1000",
        "headers": {
            "Authorization": "Bearer routed-credential",
            "Anthropic-version": "2023-06-01",
            "Anthropic-beta": "oauth-2025-04-20",
        },
        "timeout": 10,
    }


def test_cli_help_runs_the_installed_claude_help(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, HELP, "")

    monkeypatch.setattr(model_catalog.subprocess, "run", run)
    assert model_catalog.cli_help() == HELP
    assert calls == [(["claude", "--help"], {"capture_output": True, "text": True, "timeout": 20, "check": False})]


def counting(results):
    calls = []

    def read(environ):
        calls.append(environ)
        return results[len(calls) - 1]

    return read, calls


ONE = [{"model": "opus", "label": "Claude Opus 5.5", "efforts": ["high"]}]
TWO = [{"model": "sonnet", "label": "Claude Sonnet 5.5", "efforts": ["xhigh"]}]


def test_the_cache_answers_inside_the_hour_and_refreshes_after_it(monkeypatch, redis):
    read, calls = counting([ONE, TWO])
    monkeypatch.setitem(model_catalog.READERS, "claude", read)
    assert model_catalog.cached("claude", {"A": "1"}, redis, 1000.0) == ONE
    assert model_catalog.cached("claude", {"A": "1"}, redis, 1000.0 + 3599) == ONE
    assert len(calls) == 1
    assert model_catalog.cached("claude", {"A": "1"}, redis, 1000.0 + 3600) == TWO
    assert calls == [{"A": "1"}, {"A": "1"}]
    assert model_catalog.cached("claude", {"A": "1"}, redis, 1000.0 + 3601) == TWO
    assert len(calls) == 2
    assert redis.ttl(f"{model_catalog.KEY}:claude") == 3600


def test_an_empty_catalog_is_never_cached(monkeypatch, redis):
    read, calls = counting([[], ONE])
    monkeypatch.setitem(model_catalog.READERS, "codex", read)
    assert model_catalog.cached("codex", {}, redis, 1000.0) == []
    assert redis.get(f"{model_catalog.KEY}:codex") is None
    assert model_catalog.cached("codex", {}, redis, 1001.0) == ONE
    assert len(calls) == 2


def test_each_harness_has_its_own_cache_entry(monkeypatch, redis):
    monkeypatch.setitem(model_catalog.READERS, "claude", lambda environ: ONE)
    monkeypatch.setitem(model_catalog.READERS, "codex", lambda environ: TWO)
    assert model_catalog.catalog({}, redis, 1000.0) == {"claude": ONE, "codex": TWO}
    assert json.loads(redis.get(f"{model_catalog.KEY}:codex")) == {"at": 1000.0, "models": TWO}


def test_without_redis_every_call_reads_the_source(monkeypatch):
    read, calls = counting([ONE, TWO])
    monkeypatch.setitem(model_catalog.READERS, "codex", read)
    assert model_catalog.cached("codex", {}, None, 1000.0) == ONE
    assert model_catalog.cached("codex", {}, None, 1001.0) == TWO


def test_the_cache_lives_under_the_swarm_key_root():
    from scripts.swarm import keyspace

    assert model_catalog.KEY == f"{keyspace.ROOT}:swarm:models"
    assert model_catalog.TTL == 3600
