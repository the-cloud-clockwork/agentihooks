from collections.abc import Callable
from dataclasses import dataclass, replace

from hooks.classifier import code_rules, decide, decision_log
from hooks.classifier.definitions import Definition, DefinitionError, QuestionSpec, load
from hooks.classifier.questions import Choice, Question, YesNo, validate
from hooks.classifier.result import Answer, DecisionResult


@dataclass(frozen=True)
class RunResult:
    verdicts: dict[str, object]
    raw: DecisionResult
    definition: Definition

    @property
    def thresholds(self) -> dict[str, float]:
        return self.definition.thresholds


def _format(text: str, params: dict) -> str:
    try:
        return text.format_map(params)
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise DefinitionError(f"cannot format classifier question: {exc}") from exc


def _question(question: Question, params: dict) -> Question:
    instructions = _format(question.instructions, params)
    if isinstance(question, YesNo):
        return replace(
            question,
            instructions=instructions,
            true=_format(question.true, params),
            false=_format(question.false, params),
        )
    if isinstance(question, Choice):
        return replace(
            question,
            instructions=instructions,
            options={key: _format(text, params) for key, text in question.options.items()},
        )
    return replace(question, instructions=instructions, levels=[_format(text, params) for text in question.levels])


def _expanded(spec: QuestionSpec, params: dict) -> dict[str, Question]:
    if spec.each is None:
        return {spec.name: _question(spec.question, params)}
    items = params.get(spec.each)
    if not isinstance(items, list):
        raise DefinitionError(f"each parameter {spec.each} must be a list")
    return {
        f"{spec.name}_{index}": _question(spec.question, {**params, "item": item, "index": index})
        for index, item in enumerate(items)
    }


def questions_for(definition: Definition, params: dict | None = None) -> dict[str, Question]:
    params = {} if params is None else params
    if not isinstance(params, dict):
        raise DefinitionError("classifier parameters must be a mapping")
    questions = {}
    for spec in definition.questions:
        expanded = _expanded(spec, params)
        if questions.keys() & expanded.keys():
            raise DefinitionError("expanded question names must be unique")
        questions.update(expanded)
    validate(questions)
    return questions


def _verdict(answer: Answer, definition: Definition) -> object:
    rule = definition.rule
    if rule.type == "yes":
        return answer.noul >= definition.thresholds[rule.threshold]
    if rule.threshold is not None:
        if answer.confidence is None or answer.confidence < definition.thresholds[rule.threshold]:
            return None
    return answer.choice if rule.type == "choice" else answer.score


def run(
    name: str,
    state: object,
    params: dict | None = None,
    harness: str | None = None,
    *,
    decider: Callable[..., DecisionResult] | None = None,
    environ: dict | None = None,
) -> RunResult:
    try:
        definition = load(name, environ=environ)
    except DefinitionError as exc:
        with decision_log.record_context(definition=name):
            decision_log.append(name, state, None, 0, [decision_log.failure_record("definition", exc)])
        raise
    rule = code_rules.rule_for(definition)
    params = {} if params is None else params
    questions = questions_for(definition, params) if rule is None else rule.questions(definition, state, params)
    options = {
        "purpose": definition.purpose,
        "harness": harness,
        "fallbacks": None if definition.fallbacks == "cli" else [],
    }
    if decider is None:
        decider = decide
    else:
        options = {key: value for key, value in options.items() if value is not None}
    with decision_log.record_context(definition=definition.name, definition_digest=definition.digest):
        result = decider(state, questions, **options)
    if rule is not None:
        verdicts = rule.verdicts(definition, state, params, result.answers)
    elif definition.rule.type == "code":
        verdicts = {}
    else:
        verdicts = {key: _verdict(answer, definition) for key, answer in result.answers.items()}
    return RunResult(verdicts, result, definition)
