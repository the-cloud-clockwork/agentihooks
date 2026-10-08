from collections.abc import Mapping
from urllib.parse import urlsplit

from hooks.context.account_sessions import API_MARKER, CODEX_TOKEN_PREFIX, TOKEN_PREFIX

ANTHROPIC_HOST = "api.anthropic.com"
CODEX_TOKEN_ENV = "CODEX_ACCESS_TOKEN"
_CODEX_API_NAMES = frozenset({"CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", API_MARKER})
_API_NAMES = frozenset(
    {
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "AWS_BEARER_TOKEN_BEDROCK",
        "ANTHROPIC_CUSTOM_HEADERS",
        API_MARKER,
    }
)
_API_PREFIXES = ("CLAUDE_CODE_USE_", "ANTHROPIC_VERTEX_", "ANTHROPIC_FOUNDRY_")


def foreign_base_url(url: str) -> bool:
    try:
        return urlsplit(url).hostname != ANTHROPIC_HOST
    except ValueError:
        return True


def _api_name(name: str, value: str) -> bool:
    if name == "ANTHROPIC_BASE_URL":
        return foreign_base_url(value)
    return name in _API_NAMES or name.startswith(_API_PREFIXES)


def subscription_child(environ: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in environ.items() if not _api_name(name, value)}


def api_child(environ: Mapping[str, str]) -> dict[str, str]:
    child = {
        name: value
        for name, value in environ.items()
        if name != "CLAUDE_CODE_OAUTH_TOKEN" and not name.startswith(TOKEN_PREFIX)
    }
    child[API_MARKER] = "1"
    return child


def _codex_without_tokens(environ: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in environ.items()
        if name != CODEX_TOKEN_ENV and not name.startswith((CODEX_TOKEN_PREFIX, TOKEN_PREFIX))
    }


def codex_subscription_child(environ: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in _codex_without_tokens(environ).items() if name not in _CODEX_API_NAMES}


def codex_api_child(environ: Mapping[str, str]) -> dict[str, str]:
    child = _codex_without_tokens(environ)
    child[API_MARKER] = "1"
    return child
