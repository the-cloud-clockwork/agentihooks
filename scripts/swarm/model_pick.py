from dataclasses import dataclass

from hooks.classifier import ClassifierUnavailable, decide, definitions, runner
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
            {"levels": list(levels)},
            harness,
            decider=decide,
            environ=environ,
        )
    except (ClassifierUnavailable, definitions.DefinitionError):
        return default
    result = output.raw
    answer = result.answers["effort"]
    if answer.confidence < output.thresholds["confidence"]:
        return ModelPick(default.model, default.effort, result.source, answer.confidence)
    raised = max(levels.index(floor), round(max(0, min(3, answer.score))))
    return ModelPick(default.model, levels[raised], result.source, answer.confidence)
