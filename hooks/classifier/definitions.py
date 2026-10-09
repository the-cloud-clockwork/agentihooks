import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import yaml

from hooks.classifier.errors import ClassifierInputError
from hooks.classifier.questions import Choice, Question, Score, YesNo, validate
from hooks.context import profile_chain

NAME = re.compile(r"[a-z][a-z0-9_-]*\Z")
QUESTION_FIELDS = {"name", "type", "instructions", "true", "false", "options", "levels", "each"}
FIELDS = {"version", "purpose", "fallbacks", "questions", "thresholds", "rule"}


class DefinitionError(ClassifierInputError):
    pass


@dataclass(frozen=True)
class QuestionSpec:
    name: str
    question: Question
    each: str | None = None


@dataclass(frozen=True)
class VerdictRule:
    type: str
    threshold: str | None = None


@dataclass(frozen=True)
class Definition:
    name: str
    purpose: str
    fallbacks: str
    questions: tuple[QuestionSpec, ...]
    thresholds: dict[str, float]
    rule: VerdictRule
    digest: str = ""


def _mapping(raw: object, fields: set[str], label: str) -> dict:
    if not isinstance(raw, dict):
        raise DefinitionError(f"{label} must be a mapping")
    unknown = set(raw) - fields
    if unknown:
        raise DefinitionError(f"unknown {label} keys: {', '.join(sorted(str(key) for key in unknown))}")
    return raw


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DefinitionError(f"{label} must be nonempty text")
    return value


def _identifier(value: object, label: str) -> str:
    text = _text(value, label)
    if not NAME.fullmatch(text):
        raise DefinitionError(f"invalid {label}: {text}")
    return text


def _options(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or not raw:
        raise DefinitionError("choice options must be a nonempty mapping")
    return {_text(key, "option"): _text(value, "option text") for key, value in raw.items()}


def _levels(raw: object) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise DefinitionError("score levels must be a nonempty list")
    return [_text(value, "level") for value in raw]


def _question(raw: object) -> QuestionSpec:
    raw = _mapping(raw, QUESTION_FIELDS, "question")
    kind = raw.get("type")
    factories = {
        "yesno": lambda: YesNo(
            instructions, _text(raw.get("true", "Yes"), "true"), _text(raw.get("false", "No"), "false")
        ),
        "choice": lambda: Choice(instructions, _options(raw.get("options"))),
        "score": lambda: Score(instructions, _levels(raw.get("levels"))),
    }
    if kind not in factories:
        raise DefinitionError(f"unknown question type: {kind}")
    name = _text(raw.get("name"), "question name")
    instructions = _text(raw.get("instructions"), "instructions")
    each = raw.get("each")
    if each is not None:
        each = _identifier(each, "each parameter")
    return QuestionSpec(name, factories[kind](), each)


def _questions(raw: object) -> tuple[QuestionSpec, ...]:
    if not isinstance(raw, list):
        raise DefinitionError("questions must be a list")
    questions = tuple(_question(item) for item in raw)
    names = [item.name for item in questions]
    if len(set(names)) != len(names):
        raise DefinitionError("question names must be unique")
    try:
        validate({item.name: item.question for item in questions})
    except ClassifierInputError as exc:
        raise DefinitionError(str(exc)) from exc
    return questions


def _probability(value: object, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DefinitionError(f"threshold {key} must be between zero and one")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise DefinitionError(f"threshold {key} must be between zero and one")
    return float(value)


def _thresholds(raw: object) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise DefinitionError("thresholds must be a mapping")
    return {_identifier(key, "threshold key"): _probability(value, key) for key, value in raw.items()}


def _rule(raw: object, questions: tuple[QuestionSpec, ...], thresholds: dict) -> VerdictRule:
    raw = _mapping(raw, {"type", "threshold"}, "rule")
    kind = raw.get("type")
    kinds = {"yes": YesNo, "choice": Choice, "score": Score, "code": object}
    if kind not in kinds:
        raise DefinitionError(f"unknown verdict rule: {kind}")
    if not all(isinstance(item.question, kinds[kind]) for item in questions):
        raise DefinitionError(f"rule {kind} does not match its questions")
    threshold = raw.get("threshold")
    if kind == "yes" and threshold is None:
        raise DefinitionError("yes rule requires a threshold key")
    if threshold is not None and (not isinstance(threshold, str) or threshold not in thresholds):
        raise DefinitionError("rule threshold must name a defined threshold")
    return VerdictRule(kind, threshold)


def _parse(name: str, raw: object) -> Definition:
    raw = _mapping(raw, FIELDS, "definition")
    if type(raw.get("version")) is not int or raw["version"] != 1:
        raise DefinitionError("definition version must be 1")
    purpose = _text(raw.get("purpose"), "purpose")
    fallbacks = raw.get("fallbacks", "cli")
    if fallbacks not in ("cli", "none"):
        raise DefinitionError("fallbacks must be cli or none")
    questions = _questions(raw.get("questions"))
    thresholds = _thresholds(raw.get("thresholds", {}))
    rule = _rule(raw.get("rule"), questions, thresholds)
    return Definition(name, purpose, fallbacks, questions, thresholds, rule)


def _read(name: str, path: Path) -> Definition:
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise DefinitionError(f"cannot read definition {name}: {exc}") from exc
    return _parse(name, raw)


def load(name: str) -> Definition:
    name = _identifier(name, "classifier name")
    path = profile_chain.BUILT_IN_PROFILES / "package" / "classifiers" / f"{name}.yaml"
    definition = _read(name, path)
    digest = hashlib.sha256(json.dumps(asdict(definition), sort_keys=True).encode()).hexdigest()
    return replace(definition, digest=digest)
