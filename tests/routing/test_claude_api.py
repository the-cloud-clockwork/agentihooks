import pytest

from hooks.context.account_sessions import API_ACCOUNT, API_MARKER
from scripts.claude_quota_balancer import Credential, _child_environment
from scripts.routing import claude_api, envs
from scripts.routing.claude_api import ClaudeApiSource
from scripts.routing.slots import API, Slot

SENTINEL = "sentinel-value-7f3a"
NOW = 1_900_000_000


def test_a_token_route_child_carries_no_api_credentials():
    environ = {
        "HOME": "/home/u",
        "AH_CC_TOKEN_alpha": "alpha-token",
        "ANTHROPIC_AUTH_TOKEN": SENTINEL,
        "ANTHROPIC_API_KEY": SENTINEL,
        "ANTHROPIC_BASE_URL": "https://gateway.example",
        "ANTHROPIC_BEDROCK_BASE_URL": "https://bedrock.example",
        "ANTHROPIC_VERTEX_PROJECT_ID": "project",
        "ANTHROPIC_FOUNDRY_RESOURCE": SENTINEL,
        "CLAUDE_CODE_USE_BEDROCK": "1",
        API_MARKER: "1",
    }
    child = _child_environment(Credential("AH_CC_TOKEN_alpha", "alpha-token"), environ)
    assert child == {"HOME": "/home/u", "CLAUDE_CODE_OAUTH_TOKEN": "alpha-token"}


def test_a_subscription_child_keeps_the_anthropic_base_url_only():
    kept = {"HOME": "/home/u", "ANTHROPIC_BASE_URL": "https://API.anthropic.com/v1", "ANTHROPIC_MODEL": "opus"}
    assert envs.subscription_child({**kept, "CLAUDE_CODE_USE_VERTEX": "1"}) == kept
    assert envs.subscription_child({"ANTHROPIC_BASE_URL": "https://api.anthropic.com.example"}) == {}
    assert envs.subscription_child({"ANTHROPIC_BASE_URL": ""}) == {}


def test_an_api_child_has_no_oauth_token_and_carries_the_route_marker():
    environ = {
        "HOME": "/home/u",
        "ANTHROPIC_API_KEY": SENTINEL,
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth",
        "AH_CC_TOKEN_alpha": "alpha-token",
        "AH_CC_TOKEN_": "bare",
    }
    child = ClaudeApiSource().child_env(Slot("claude", API_ACCOUNT, 0, 0, kind=API), environ)
    assert child == {"HOME": "/home/u", "ANTHROPIC_API_KEY": SENTINEL, API_MARKER: "1"}
    assert API_MARKER not in environ


@pytest.mark.parametrize(
    ("environ", "label"),
    [
        ({"CLAUDE_CODE_USE_BEDROCK": "1"}, "bedrock"),
        ({"CLAUDE_CODE_USE_VERTEX": "true"}, "vertex"),
        ({"CLAUDE_CODE_USE_FOUNDRY": "1"}, "foundry"),
        ({"CLAUDE_CODE_USE_FOUNDRY": " Yes "}, "foundry"),
        ({"CLAUDE_CODE_USE_BEDROCK": "ON"}, "bedrock"),
        ({"CLAUDE_CODE_USE_BEDROCK": "off"}, ""),
        ({"CLAUDE_CODE_USE_BEDROCK": "no"}, ""),
        ({"CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_API_KEY": SENTINEL}, "bedrock"),
        ({"CLAUDE_CODE_USE_BEDROCK": "0", "CLAUDE_CODE_USE_VERTEX": "1"}, "vertex"),
        ({"ANTHROPIC_BASE_URL": "https://gateway.example", "ANTHROPIC_AUTH_TOKEN": SENTINEL}, "gateway"),
        ({"ANTHROPIC_BASE_URL": "https://gateway.example", "ANTHROPIC_API_KEY": SENTINEL}, "gateway"),
        ({"ANTHROPIC_API_KEY": SENTINEL}, "anthropic-key"),
        ({"ANTHROPIC_BASE_URL": "", "ANTHROPIC_API_KEY": SENTINEL}, "anthropic-key"),
        ({"ANTHROPIC_BASE_URL": "https://gateway.example"}, ""),
        ({"ANTHROPIC_AUTH_TOKEN": SENTINEL}, ""),
        ({"ANTHROPIC_API_KEY": ""}, ""),
        ({"CLAUDE_CODE_USE_BEDROCK": "0"}, ""),
        ({"CLAUDE_CODE_USE_BEDROCK": " False "}, ""),
        ({"CLAUDE_CODE_USE_BEDROCK": ""}, ""),
        ({"AH_CC_TOKEN_alpha": "alpha-token"}, ""),
    ],
)
def test_the_provider_label_names_the_api_kind(environ, label):
    assert claude_api.provider(environ) == label


def test_an_api_endpoint_offers_one_api_slot_without_a_value():
    environ = {"ANTHROPIC_BASE_URL": "https://gateway.example", "ANTHROPIC_AUTH_TOKEN": SENTINEL}
    slots = ClaudeApiSource({"api": 2, "alpha": 5}).slots(environ, NOW)
    assert slots == [Slot("claude", "api", 0, 2, kind="api", provider="gateway")]
    assert ClaudeApiSource().slots({"CLAUDE_CODE_USE_BEDROCK": "1"}, NOW) == [
        Slot("claude", "api", 0, 0, kind="api", provider="bedrock")
    ]
    assert SENTINEL not in repr(slots)


def test_no_api_endpoint_offers_no_slot():
    assert ClaudeApiSource({"api": 1}).slots({"AH_CC_TOKEN_alpha": "alpha-token"}, NOW) == []
