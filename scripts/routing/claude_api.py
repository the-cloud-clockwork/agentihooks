from collections.abc import Mapping
from dataclasses import dataclass, field

from hooks.context.account_sessions import API_ACCOUNT
from scripts.routing import envs
from scripts.routing.slots import API, Slot

HARNESS = "claude"
_PLATFORMS = (
    ("CLAUDE_CODE_USE_BEDROCK", "bedrock"),
    ("CLAUDE_CODE_USE_VERTEX", "vertex"),
    ("CLAUDE_CODE_USE_FOUNDRY", "foundry"),
)
_OFF = frozenset({"", "0", "false"})


def provider(environ: Mapping[str, str]) -> str:
    for name, label in _PLATFORMS:
        if environ.get(name, "").strip().lower() not in _OFF:
            return label
    if environ.get("ANTHROPIC_BASE_URL") and (environ.get("ANTHROPIC_AUTH_TOKEN") or environ.get("ANTHROPIC_API_KEY")):
        return "gateway"
    if environ.get("ANTHROPIC_API_KEY"):
        return "anthropic-key"
    return ""


@dataclass(frozen=True)
class ClaudeApiSource:
    sessions: Mapping[str, int] = field(default_factory=dict)

    def slots(self, environ: Mapping[str, str], now: float) -> list[Slot]:
        label = provider(environ)
        if not label:
            return []
        return [Slot(HARNESS, API_ACCOUNT, 0, self.sessions.get(API_ACCOUNT, 0), kind=API, provider=label)]

    def child_env(self, slot: Slot, environ: Mapping[str, str]) -> dict[str, str]:
        return envs.api_child(environ)
