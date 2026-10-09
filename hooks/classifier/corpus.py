from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from hooks.classifier import definitions
from hooks.classifier.api import DecisionsApiBackend
from hooks.classifier.definitions import Definition, DefinitionError
from hooks.classifier.errors import BackendFailure, ClassifierRequestError
from hooks.classifier.fallbacks import cli_backends
from hooks.classifier.questions import Choice, Question
from hooks.classifier.result import Answer, DecisionRequest
from hooks.classifier.runner import _verdict, questions_for
from hooks.classifier.settings import api_configured
from hooks.classifier.settings import load as load_settings
from scripts.swarm import metrics, metrics_outbox

CASE_FIELDS = {"name", "state", "params", "expected", "control", "samples"}
SAMPLE_FIELDS = {"source", "latency_ms", "answers"}
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


class CorpusError(DefinitionError):
    pass


@dataclass(frozen=True)
class Sample:
    source: str
    latency_ms: int
    answers: dict[str, Answer]


@dataclass(frozen=True)
class Case:
    name: str
    state: object
    params: dict
    expected: dict
    control: bool
    samples: tuple[Sample, ...]


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


def _score(cases: tuple[Case, ...], outcomes: list[Outcome]) -> dict:
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
            **_score(self.cases, list(self.outcomes)),
            "backends": {
                source: _score(self.cases, [item for item in self.outcomes if item.source == source])
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


def path_for(name: str) -> Path:
    selected = [path for path in definitions._paths(name) if path.is_file()][-1]
    return selected.with_name(f"{name}.corpus.yaml")


def _mapping(raw: object, fields: set[str], label: str) -> dict:
    if not isinstance(raw, dict):
        raise CorpusError(f"{label} must be a mapping")
    unknown = set(raw) - fields
    if unknown:
        raise CorpusError(f"unknown {label} keys: {', '.join(sorted(str(key) for key in unknown))}")
    return raw


def _verdict_allowed(definition: Definition, question: Question, value: object) -> bool:
    if definition.rule.type == "yes":
        return isinstance(value, bool)
    if value is None:
        return definition.rule.threshold is not None
    if isinstance(question, Choice):
        return value in question.options
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(bound, (int, float)) and not isinstance(bound, bool) for bound in value)
        and 0 <= value[0] <= value[1] <= 1
    )


def _expected(definition: Definition, case: str, raw: object, questions: dict[str, Question]) -> dict:
    if not isinstance(raw, dict) or set(raw) != set(questions):
        raise CorpusError(f"case {case} expected must name exactly: {', '.join(sorted(questions))}")
    for key, value in raw.items():
        if not _verdict_allowed(definition, questions[key], value):
            raise CorpusError(f"case {case} expected {key} is not a verdict its rule can give")
    return dict(raw)


def _sample(case: str, index: int, raw: object, questions: dict[str, Question]) -> Sample:
    label = f"case {case} sample {index}"
    raw = _mapping(raw, SAMPLE_FIELDS, label)
    source, latency, answers = raw.get("source"), raw.get("latency_ms", 0), raw.get("answers")
    if not isinstance(source, str) or not source:
        raise CorpusError(f"{label} needs a source")
    if type(latency) is not int or latency < 0:
        raise CorpusError(f"{label} latency_ms must be a whole number of milliseconds")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise CorpusError(f"{label} answers must name exactly: {', '.join(sorted(questions))}")
    for key, question in questions.items():
        answer = answers[key]
        if not isinstance(answer, dict) or answer.get("type") != question.type or answer.get(question.type) is None:
            raise CorpusError(f"{label} answer {key} must be a {question.type} answer")
    return Sample(source, latency, {key: Answer.from_wire(answers[key]) for key in questions})


def _case(definition: Definition, raw: object) -> Case:
    raw = _mapping(raw, CASE_FIELDS, "case")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise CorpusError("case name must be nonempty text")
    if "state" not in raw:
        raise CorpusError(f"case {name} needs a state")
    params = raw.get("params", {})
    if not isinstance(params, dict):
        raise CorpusError(f"case {name} params must be a mapping")
    questions = questions_for(definition, params)
    expected = _expected(definition, name, raw.get("expected"), questions)
    control = raw.get("control", False)
    if not isinstance(control, bool):
        raise CorpusError(f"case {name} control must be true or false")
    if control and any(value is not False and value is not None for value in expected.values()):
        raise CorpusError(f"control case {name} must expect only rejections")
    samples = raw.get("samples")
    if not isinstance(samples, list) or not samples:
        raise CorpusError(f"case {name} needs at least one recorded sample")
    recorded = tuple(_sample(name, index, item, questions) for index, item in enumerate(samples))
    return Case(name, raw["state"], params, expected, control, recorded)


def load(definition: Definition, path: Path) -> tuple[Case, ...]:
    if definition.rule.type == "code":
        raise CorpusError(f"classifier {definition.name} keeps a code rule; replay needs a yes, choice or score rule")
    if not path.is_file():
        raise CorpusError(f"no corpus for classifier {definition.name}: {path}")
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise CorpusError(f"cannot read corpus {definition.name}: {exc}") from exc
    raw = _mapping(raw, {"version", "cases"}, "corpus")
    if type(raw.get("version")) is not int or raw["version"] != 1:
        raise CorpusError("corpus version must be 1")
    if not isinstance(raw.get("cases"), list) or not raw["cases"]:
        raise CorpusError("corpus cases must be a nonempty list")
    cases = tuple(_case(definition, item) for item in raw["cases"])
    names = [case.name for case in cases]
    if len(set(names)) != len(names):
        raise CorpusError("case names must be unique")
    return cases


def _verdicts(definition: Definition, answers: dict[str, Answer]) -> dict:
    return {key: _verdict(answer, definition) for key, answer in answers.items()}


def replay(definition: Definition, cases: tuple[Case, ...]) -> tuple[Outcome, ...]:
    return tuple(
        Outcome(case, sample.source, index, sample.latency_ms, _verdicts(definition, sample.answers))
        for case in cases
        for index, sample in enumerate(case.samples)
    )


def live_backends() -> list:
    settings = load_settings()
    api = (
        [DecisionsApiBackend(model, settings.url, settings.timeout_s) for model in settings.models]
        if api_configured(settings)
        else []
    )
    return [*api, *cli_backends("claude")]


def _ask(backend, definition: Definition, case: Case) -> tuple[dict | None, int]:
    started = time.monotonic()
    try:
        result = backend.decide(DecisionRequest(case.state, questions_for(definition, case.params)))
        verdicts = _verdicts(definition, result.answers)
    except (BackendFailure, ClassifierRequestError):
        verdicts = None
    return verdicts, int((time.monotonic() - started) * 1000)


def live(definition: Definition, cases: tuple[Case, ...], backends: list, repeats: int) -> tuple[Outcome, ...]:
    if os.environ.get("CI"):
        raise CorpusError("live classifier runs are refused in CI")
    outcomes = []
    for backend in backends:
        for case in cases:
            for index in range(repeats):
                verdicts, latency = _ask(backend, definition, case)
                outcomes.append(Outcome(case, backend.name, index, latency, verdicts))
    return tuple(outcomes)


def evaluate(name: str, repeats: int = 0, backends: list | None = None) -> Evaluation:
    definition = definitions.load(name)
    cases = load(definition, path_for(name))
    if not repeats:
        return Evaluation(definition, cases, "replay", replay(definition, cases))
    chosen = live_backends() if backends is None else backends
    return Evaluation(definition, cases, "live", live(definition, cases, chosen, repeats))


def record(evaluation: Evaluation, now_ms: int, environ=os.environ) -> list[str]:
    rows = evaluation.rows(now_ms, environ.get("AGENTIHOOKS_SWARM") or "local")
    return metrics.record(EVALS, rows, now_ms, environ)
