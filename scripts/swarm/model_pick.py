from dataclasses import dataclass

from hooks.classifier import ClassifierUnavailable, Score, decide

EFFORTS = {"claude": ("low", "medium", "high", "max"), "codex": ("low", "medium", "high", "xhigh")}


@dataclass(frozen=True)
class ModelPick:
    model: str
    effort: str
    source: str = "lane-default"
    confidence: float | None = None


def pick(harness: str, lane: dict, task: dict, environ: dict) -> ModelPick:
    from scripts.init_agent import model_effort

    default = ModelPick(lane.get("model", ""), lane.get("effort", ""))
    levels = EFFORTS[harness]
    floor = model_effort(harness, [], environ)[1]
    if default.effort != "auto" or floor not in levels:
        return default
    try:
        result = decide(
            {
                "title": task.get("title", ""),
                "description": task.get("description", ""),
                "kind": task.get("kind", "code"),
                "territory_size": len(task.get("territory", [])),
            },
            {"effort": Score("How much reasoning does this task need?", list(levels))},
            purpose="model-pick",
            harness=harness,
        )
    except ClassifierUnavailable:
        return default
    answer = result.answers["effort"]
    if answer.confidence < float(environ.get("AGENTIHOOKS_MODEL_PICK_MIN_CONFIDENCE", "0.6")):
        return ModelPick(default.model, default.effort, result.source, answer.confidence)
    raised = max(levels.index(floor), round(max(0, min(3, answer.score))))
    return ModelPick(default.model, levels[raised], result.source, answer.confidence)
