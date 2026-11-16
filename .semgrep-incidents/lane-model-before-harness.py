from dataclasses import replace

from scripts.swarm.model_pick import AUTO, CLAUDE_ONLY, EFFORTS, ModelPick


def unresolved_lane(harness: str, lane: dict[str, str]) -> ModelPick:
    # ruleid: lane-model-before-harness
    default = ModelPick(lane.get("model", ""), lane.get("effort", ""))
    if default.effort != AUTO or harness not in EFFORTS:
        return default
    return default


def resolved_lane(harness: str, lane: dict[str, str], default_model: str) -> ModelPick:
    # ok: lane-model-before-harness
    default = ModelPick(lane.get("model", ""), lane.get("effort", ""))
    if harness == "codex" and default.model.startswith(CLAUDE_ONLY):
        default = replace(default, model=default_model)
    if default.effort != AUTO or harness not in EFFORTS:
        return default
    return default
