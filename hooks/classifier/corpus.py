from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from hooks.classifier import definitions
from hooks.classifier.definitions import Definition, DefinitionError
from hooks.classifier.questions import Choice, Question
from hooks.classifier.result import Answer
from hooks.classifier.runner import questions_for

CASE_FIELDS = {"name", "state", "params", "expected", "control", "samples"}
SAMPLE_FIELDS = {"source", "latency_ms", "answers"}


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
