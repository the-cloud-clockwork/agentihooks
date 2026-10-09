from collections.abc import Callable
from dataclasses import dataclass, replace

from hooks.classifier import decide, decision_log
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


def _named(spec: QuestionSpec, params: dict, default: str) -> str:
    return default if spec.key is None else _format(spec.key, params)


def _expanded(group: list[QuestionSpec], params: dict) -> list[tuple[str, Question]]:
    each = group[0].each
    if each is None:
        return [(_named(group[0], params, group[0].name), _question(group[0].question, params))]
    items = params.get(each)
    if not isinstance(items, list):
        raise DefinitionError(f"each parameter {each} must be a list")
    expanded = []
    for index, item in enumerate(items):
        scope = {**params, "item": item, "index": index}
        expanded += [(_named(spec, scope, f"{spec.name}_{index}"), _question(spec.question, scope)) for spec in group]
    return expanded


def _groups(specs: tuple[QuestionSpec, ...]) -> list[list[QuestionSpec]]:
    groups, lists = [], {}
    for spec in specs:
        if spec.each is None:
            groups.append([spec])
        elif spec.each in lists:
            lists[spec.each].append(spec)
        else:
            lists[spec.each] = [spec]
            groups.append(lists[spec.each])
    return groups


def questions_for(definition: Definition, params: dict | None = None) -> dict[str, Question]:
    params = {} if params is None else params
    if not isinstance(params, dict):
        raise DefinitionError("classifier parameters must be a mapping")
    questions = {}
    for group in _groups(definition.questions):
        for name, question in _expanded(group, params):
            if name in questions:
                raise DefinitionError("expanded question names must be unique")
            questions[name] = question
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
    questions = questions_for(definition, params)
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
    verdicts = (
        {}
        if definition.rule.type == "code"
        else {key: _verdict(answer, definition) for key, answer in result.answers.items()}
    )
    return RunResult(verdicts, result, definition)
