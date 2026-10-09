from __future__ import annotations

import os
from dataclasses import dataclass

from hooks.classifier.api import KEY_VAR

DEFAULT_MODELS = ("liquid-d1", "jev-1.13", "pplx-decider-v1-27b")
MODEL_CONTEXT_TOKENS = {
    "pplx-decider-v1-27b": 262_144,
    "liquid-d1": 32_768,
    "jev-1.13": 32_768,
}


@dataclass(frozen=True)
class Settings:
    url: str
    models: tuple
    timeout_s: float
    down_ttl_s: float


def load() -> Settings:
    models = os.environ.get("AGENTIHOOKS_CLASSIFIER_MODELS", "")
    return Settings(
        url=os.environ.get("AGENTIHOOKS_CLASSIFIER_URL", "").strip(),
        models=tuple(m.strip() for m in models.split(",") if m.strip()) or DEFAULT_MODELS,
        timeout_s=float(os.environ.get("AGENTIHOOKS_CLASSIFIER_TIMEOUT_S", "5")),
        down_ttl_s=float(os.environ.get("AGENTIHOOKS_CLASSIFIER_DOWN_TTL_S", "120")),
    )


def api_configured(settings: Settings) -> bool:
    return bool(settings.url and os.environ.get(KEY_VAR))
