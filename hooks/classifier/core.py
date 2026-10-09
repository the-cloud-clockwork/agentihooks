from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import replace
from typing import Protocol

from hooks.classifier import decision_log, down_cache
from hooks.classifier.api import DecisionsApiBackend
from hooks.classifier.errors import BackendFailure, ClassifierRequestError, ClassifierUnavailable
from hooks.classifier.fallbacks import cli_backends
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


def _ask_api(
    request: DecisionRequest,
    settings: Settings,
    failures: list,
    api_down_cached: bool,
) -> DecisionResult | None:
    if not api_configured(settings):
        return None
    if api_down_cached:
        failures.extend(down_cache.failures())
        return None
    backends = api_backends(request, settings)
    refused = False
    for backend in backends:
        try:
            return backend.decide(request)
        except BackendFailure as failure:
            failures.append(decision_log.failure_record(backend.name, failure))
            if failure.skip_api:
                refused = True
                break
        except ClassifierRequestError as failure:
            failures.append(decision_log.failure_record(backend.name, failure))
            raise
    if backends and (refused or len(backends) == len(settings.models)):
        down_cache.mark_down(failures)
    return None


def _ask_fallbacks(request: DecisionRequest, fallbacks: Sequence[Backend], failures: list) -> DecisionResult | None:
    for backend in fallbacks:
        try:
            return backend.decide(request)
        except BackendFailure as failure:
            failures.append(decision_log.failure_record(getattr(backend, "model", backend.name), failure))
    return None


def decide(
    state: object,
    questions: dict,
    *,
    purpose: str,
    harness: str | None = None,
    fallbacks: Sequence[Backend] | None = None,
) -> DecisionResult:
    validate(questions)
    request = DecisionRequest(state, questions)
    settings = load()
    cached = api_configured(settings) and down_cache.is_down(settings.down_ttl_s)
    failures = []
    result = None
    started = time.monotonic()
    try:
        result = _ask_api(request, settings, failures, cached)
        if result is None:
            result = _ask_fallbacks(request, cli_backends(harness) if fallbacks is None else fallbacks, failures)
    finally:
        latency_ms = int((time.monotonic() - started) * 1000)
        decision_log.append(purpose, state, result, latency_ms, failures, cached)
    if result is None:
        raise ClassifierUnavailable("no decision backend answered")
    return replace(result, latency_ms=latency_ms)
