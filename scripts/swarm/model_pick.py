from dataclasses import dataclass

from hooks.classifier import Answer, ClassifierUnavailable, code_rules, decide, definitions, runner
from scripts.swarm.effort_range import EFFORTS

PURPOSE = "model-pick"


@dataclass(frozen=True)
class ModelPick:
    model: str
    effort: str
    source: str = "lane-default"
    confidence: float | None = None


def frontier(harness: str) -> ModelPick:
    from scripts.init_agent import EFFORT_DEFAULT, MODEL_DEFAULTS

    return ModelPick(MODEL_DEFAULTS[harness], EFFORT_DEFAULT, "frontier")


def pick(harness: str, lane: dict, task: dict, environ: dict) -> ModelPick:
    from scripts.init_agent import model_effort

    default = ModelPick(lane.get("model", ""), lane.get("effort", ""))
    levels = EFFORTS[harness]
    floor = model_effort(harness, [], environ)[1]
    if default.effort != "auto" or floor not in levels:
        return default
    try:
        output = runner.run(
            PURPOSE,
            {
                "title": task.get("title", ""),
                "description": task.get("description", ""),
                "kind": task.get("kind", "code"),
                "territory_size": len(task.get("territory", [])),
            },
            {"levels": list(levels), "floor": floor},
            harness,
            decider=decide,
            environ=environ,
        )
    except (ClassifierUnavailable, definitions.DefinitionError):
        return default
    result = output.raw
    answer = result.answers["effort"]
    raised = effort(answer, list(levels), floor, output.thresholds["confidence"])
    return ModelPick(default.model, raised or default.effort, result.source, answer.confidence)


def effort(answer: Answer, levels: list, floor: str, confidence: float) -> str | None:
    if answer.confidence < confidence:
        return None
    return levels[max(levels.index(floor), round(max(0, min(3, answer.score))))]


def _verdicts(definition, state, params, answers):
    picked = effort(answers["effort"], params["levels"], params["floor"], definition.thresholds["confidence"])
    return {"effort": picked or LANE_DEFAULT}


LANE_DEFAULT = "lane default"
RULE = code_rules.CodeRule(
    code_rules.asked,
    _verdicts,
    {"effort": (LANE_DEFAULT, *sorted({level for levels in EFFORTS.values() for level in levels}))},
    {"effort": LANE_DEFAULT},
)
