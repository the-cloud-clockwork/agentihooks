from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import replace
from typing import Protocol

from hooks.classifier import decision_log, down_cache
from hooks.classifier.api import DecisionsApiBackend
from hooks.classifier.errors import BackendFailure, ClassifierUnavailable
from hooks.classifier.questions import validate
from hooks.classifier.result import DecisionRequest, DecisionResult
from hooks.classifier.settings import MODEL_CONTEXT_TOKENS, Settings, api_configured, load


class Backend(Protocol):
    name: str

    def decide(self, request: DecisionRequest) -> DecisionResult: ...


def api_backends(request: DecisionRequest, settings: Settings) -> list:
    needed = request.estimated_tokens()
    return [
        DecisionsApiBackend(model, settings.url, settings.timeout_s)
        for model in settings.models
        if MODEL_CONTEXT_TOKENS.get(model, needed) >= needed
    ]


def _ask_api(request: DecisionRequest, settings: Settings) -> DecisionResult | None:
    if not api_configured(settings) or down_cache.is_down(settings.down_ttl_s):
        return None
    backends = api_backends(request, settings)
    for backend in backends:
        try:
            return backend.decide(request)
        except BackendFailure as failure:
            if failure.skip_api:
                break
    if backends:
        down_cache.mark_down()
    return None


def _ask_fallbacks(request: DecisionRequest, fallbacks: Sequence[Backend]) -> DecisionResult | None:
    for backend in fallbacks:
        try:
            return backend.decide(request)
        except BackendFailure:
            continue
    return None


def decide(
    state: object,
    questions: dict,
    *,
    purpose: str,
    fallbacks: Sequence[Backend] = (),
) -> DecisionResult:
    validate(questions)
    request = DecisionRequest(state, questions)
    started = time.monotonic()
    result = _ask_api(request, load()) or _ask_fallbacks(request, fallbacks)
    latency_ms = int((time.monotonic() - started) * 1000)
    decision_log.append(purpose, state, result, latency_ms)
    if result is None:
        raise ClassifierUnavailable("no decision backend answered")
    return replace(result, latency_ms=latency_ms)
