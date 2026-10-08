import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from hooks.context.account_sessions import API_ACCOUNT
from scripts.routing import envs
from scripts.routing.slots import API, Slot

HARNESS = "codex"
PROVIDER = "agentihooks-api"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
KEY_NAMES = ("CODEX_API_KEY", "OPENAI_API_KEY")
BASE_URL_NAMES = ("AH_CX_API_BASE_URL", "OPENAI_BASE_URL")


def key_name(environ: Mapping[str, str]) -> str:
    return next((name for name in KEY_NAMES if environ.get(name)), "")


def base_url_name(environ: Mapping[str, str]) -> str:
    return next((name for name in BASE_URL_NAMES if environ.get(name)), "")


def base_url(environ: Mapping[str, str]) -> str:
    return environ.get(base_url_name(environ), "")


def carries_credentials(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return bool(parts.username or parts.password or parts.query or parts.fragment)
    except ValueError:
        return True


def provider(environ: Mapping[str, str]) -> str:
    if not key_name(environ):
        return ""
    return "gateway" if base_url(environ) else "openai-key"


def overrides(key_env: str, base: str) -> list[str]:
    settings = {"name": PROVIDER, "base_url": base or DEFAULT_BASE_URL, "env_key": key_env, "wire_api": "responses"}
    pairs = [f"model_provider={json.dumps(PROVIDER)}"]
    pairs += [f"model_providers.{PROVIDER}.{key}={json.dumps(value)}" for key, value in settings.items()]
    return [part for pair in pairs for part in ("-c", pair)]


@dataclass(frozen=True)
class CodexApiSource:
    sessions: Mapping[str, int] = field(default_factory=dict)

    def slots(self, environ: Mapping[str, str], now: float) -> list[Slot]:
        label = provider(environ)
        if not label:
            return []
        return [Slot(HARNESS, API_ACCOUNT, 0, self.sessions.get(API_ACCOUNT, 0), kind=API, provider=label)]

    def child_env(self, slot: Slot, environ: Mapping[str, str]) -> dict[str, str]:
        return envs.codex_api_child(environ)
