from __future__ import annotations

import os
import time
from dataclasses import dataclass

from hooks.classifier import definitions
from hooks.classifier.api import DecisionsApiBackend
from hooks.classifier.core import Backend
from hooks.classifier.corpus import Case, CorpusError, load, path_for, questions, rule_of
from hooks.classifier.definitions import Definition
from hooks.classifier.errors import BackendFailure, ClassifierRequestError
from hooks.classifier.fallbacks import ClaudeCliBackend, CodexCliBackend
from hooks.classifier.result import Answer, DecisionRequest
from hooks.classifier.runner import _verdict
from hooks.classifier.settings import api_configured
from hooks.classifier.settings import load as load_settings
from scripts.swarm import metrics, metrics_outbox

EVALS = metrics_outbox.Table(
    "classifier_evals",
    (
        ("classifier", "String"),
        ("digest", "String"),
        ("mode", "String"),
        ("backend", "String"),
        ("corpus_case", "String"),
        ("sample", "Int64"),
        ("control", "Int64"),
        ("outcome", "String"),
        ("latency_ms", "Int64"),
    ),
)


@dataclass(frozen=True)
class Outcome:
    case: Case
    source: str
    sample: int
    latency_ms: int
    verdicts: dict | None

    @property
    def outcome(self) -> str:
        if self.verdicts is None:
            return "failed"
        hit = all(_matches(want, self.verdicts[key]) for key, want in self.case.expected.items())
        return "hit" if hit else "miss"


def _matches(expected: object, verdict: object) -> bool:
    if isinstance(expected, list):
        return isinstance(verdict, (int, float)) and expected[0] <= verdict <= expected[1]
    return verdict == expected


def score(cases: tuple[Case, ...], outcomes: list[Outcome]) -> dict:
    answered = [item for item in outcomes if item.verdicts is not None]
    wrong = {item.case.name for item in answered if item.outcome == "miss"}
    seen = {item.case.name for item in answered}
    latencies = sorted(item.latency_ms for item in answered)
    return {
        "samples": len(answered),
        "wrong": sum(item.outcome == "miss" for item in answered),
        "failures": len(outcomes) - len(answered),
        "wrong_cases": sorted(wrong),
        "held_controls": sorted(case.name for case in cases if case.control and case.name in seen - wrong),
        "latency_ms": {
            "p50": latencies[(len(latencies) - 1) // 2] if latencies else None,
            "max": latencies[-1] if latencies else None,
        },
    }


@dataclass(frozen=True)
class Evaluation:
    definition: Definition
    cases: tuple[Case, ...]
    mode: str
    outcomes: tuple[Outcome, ...]

    def report(self) -> dict:
        sources = sorted({item.source for item in self.outcomes})
        return {
            "classifier": self.definition.name,
            "digest": self.definition.digest,
            "mode": self.mode,
            "cases": len(self.cases),
            "controls": sum(case.control for case in self.cases),
            **score(self.cases, list(self.outcomes)),
            "backends": {
                source: score(self.cases, [item for item in self.outcomes if item.source == source])
                for source in sources
            },
        }

    def rows(self, now_ms: int, ledger: str) -> list[dict]:
        name = self.definition.name
        return [
            {
                "event_id": f"classifier-eval:{name}:{self.mode}:{now_ms}:{item.source}:{item.case.name}:{item.sample}",
                "ledger": ledger,
                "ts_ms": now_ms,
                **dict.fromkeys(("plan", "phase", "slice", "task"), ""),
                "classifier": name,
                "digest": self.definition.digest,
                "mode": self.mode,
                "backend": item.source,
                "corpus_case": item.case.name,
                "sample": item.sample,
                "control": int(item.case.control),
                "outcome": item.outcome,
                "latency_ms": item.latency_ms,
            }
            for item in self.outcomes
        ]


def _verdicts(definition: Definition, case: Case, answers: dict[str, Answer]) -> dict:
    rule = rule_of(definition)
    if rule is not None:
        return rule.verdicts(definition, case.state, case.params, answers)
    return {key: _verdict(answer, definition) for key, answer in answers.items()}


def replay(definition: Definition, cases: tuple[Case, ...]) -> tuple[Outcome, ...]:
    return tuple(
        Outcome(case, sample.source, index, sample.latency_ms, _verdicts(definition, case, sample.answers))
        for case in cases
        for index, sample in enumerate(case.samples)
    )


def baseline(cases: tuple[Case, ...]) -> tuple[Outcome, ...]:
    return tuple(
        Outcome(case, "baseline", index, 0, verdicts) for case in cases for index, verdicts in enumerate(case.baseline)
    )


def live_backends() -> list[Backend]:
    settings = load_settings()
    api = (
        [DecisionsApiBackend(model, settings.url, settings.timeout_s) for model in settings.models]
        if api_configured(settings)
        else []
    )
    return [*api, ClaudeCliBackend(), CodexCliBackend()]


def _ask(backend: Backend, definition: Definition, case: Case) -> tuple[dict | None, int]:
    started = time.monotonic()
    try:
        asked = questions(definition, rule_of(definition), case.state, case.params)
        result = backend.decide(DecisionRequest(case.state, asked))
        verdicts = _verdicts(definition, case, result.answers)
    except (BackendFailure, ClassifierRequestError):
        verdicts = None
    return verdicts, int((time.monotonic() - started) * 1000)


def live(definition: Definition, cases: tuple[Case, ...], backends: list[Backend], repeats: int) -> tuple[Outcome, ...]:
    if "CI" in os.environ:
        raise CorpusError("live classifier runs are refused in CI")
    outcomes = []
    for backend in backends:
        for case in cases:
            for index in range(repeats):
                verdicts, latency = _ask(backend, definition, case)
                outcomes.append(Outcome(case, backend.name, index, latency, verdicts))
    return tuple(outcomes)


def evaluate(name: str, repeats: int = 0, backends: list[Backend] | None = None) -> Evaluation:
    definition = definitions.load(name)
    cases = load(definition, path_for(name))
    if not repeats:
        return Evaluation(definition, cases, "replay", replay(definition, cases))
    chosen = live_backends() if backends is None else backends
    return Evaluation(definition, cases, "live", live(definition, cases, chosen, repeats))


def record(evaluation: Evaluation, now_ms: int, environ=os.environ) -> list[str]:
    rows = evaluation.rows(now_ms, environ.get("AGENTIHOOKS_SWARM") or "local")
    return metrics.record(EVALS, rows, now_ms, environ)
