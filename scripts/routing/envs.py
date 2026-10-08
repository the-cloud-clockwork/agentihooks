from collections.abc import Mapping
from urllib.parse import urlsplit

from hooks.context.account_sessions import API_MARKER, TOKEN_PREFIX

ANTHROPIC_HOST = "api.anthropic.com"
_API_NAMES = frozenset({"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BEDROCK_BASE_URL", API_MARKER})
_API_PREFIXES = ("CLAUDE_CODE_USE_", "ANTHROPIC_VERTEX_")


def _api_name(name: str, value: str) -> bool:
    if name == "ANTHROPIC_BASE_URL":
        return urlsplit(value).hostname != ANTHROPIC_HOST
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
