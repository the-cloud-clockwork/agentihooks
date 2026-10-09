from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass

from hooks.classifier.definitions import Definition
from hooks.classifier.questions import Question
from hooks.classifier.result import Answer

RULES = {
    "intent-check": "scripts.gates.intent:RULE",
    "intent-check-tests-first": "scripts.gates.intent:RULE",
    "model-pick": "scripts.swarm.model_pick:RULE",
    "profile-pick": "scripts.swarm.profile_choice:RULE",
    "task-difficulty": "scripts.swarm.difficulty:RULE",
}


@dataclass(frozen=True)
class CodeRule:
    questions: Callable[[Definition, object, dict], dict[str, Question]]
    verdicts: Callable[[Definition, object, dict, dict[str, Answer]], dict]
    values: dict[str, tuple]
    rejections: dict[str, object]


def asked(definition: Definition, state: object, params: dict) -> dict[str, Question]:
    from hooks.classifier.runner import questions_for

    return questions_for(definition, params)


def rule_for(definition: Definition) -> CodeRule | None:
    target = RULES.get(definition.name) if definition.rule.type == "code" else None
    if target is None:
        return None
    module, attribute = target.split(":")
    return getattr(importlib.import_module(module), attribute)
