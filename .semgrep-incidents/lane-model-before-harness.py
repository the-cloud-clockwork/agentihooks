from dataclasses import replace

from scripts.swarm.model_pick import AUTO, CLAUDE_PREFIXES, EFFORTS, ModelPick, foreign


def unresolved_lane(harness: str, lane: dict[str, str]) -> ModelPick:
    # ruleid: lane-model-before-harness
    default = ModelPick(lane.get("model", ""), lane.get("effort", ""))
    if default.effort != AUTO or harness not in EFFORTS:
        return default
    return default


def unresolved_lane_after_other_work(harness: str, lane: dict[str, str]) -> ModelPick:
    # ruleid: lane-model-before-harness
    default = ModelPick(lane.get("model", ""), lane.get("effort", ""))
    effort = default.effort.strip()
    if default.effort != AUTO or harness not in EFFORTS:
        return default
    return replace(default, effort=effort)


def resolved_after_the_return(harness: str, lane: dict[str, str], default_model: str) -> ModelPick:
    # ruleid: lane-model-before-harness
    default = ModelPick(lane.get("model", ""), lane.get("effort", ""))
    if default.effort != AUTO or harness not in EFFORTS:
        return default
    if harness == "codex" and default.model.startswith(CLAUDE_PREFIXES):
        default = replace(default, model=default_model)
    return default


def resolved_for_the_claude_harness(harness: str, lane: dict[str, str], default_model: str) -> ModelPick:
    # ruleid: lane-model-before-harness
    default = ModelPick(lane.get("model", ""), lane.get("effort", ""))
    if harness == "claude" and default.model.startswith(CLAUDE_PREFIXES):
        default = replace(default, model=default_model)
    if default.effort != AUTO or harness not in EFFORTS:
        return default
    return default


def resolved_lane(harness: str, lane: dict[str, str], default_model: str) -> ModelPick:
    # ok: lane-model-before-harness
    default = ModelPick(lane.get("model", ""), lane.get("effort", ""))
    if harness == "codex" and default.model.startswith(CLAUDE_PREFIXES):
        default = replace(default, model=default_model)
    if default.effort != AUTO or harness not in EFFORTS:
        return default
    return default


def resolved_for_either_harness(harness: str, lane: dict[str, str], default_model: str) -> ModelPick:
    # ok: lane-model-before-harness
    default = ModelPick(lane.get("model", ""), lane.get("effort", ""))
    if foreign(harness, default.model):
        default = replace(default, model=default_model)
    if default.effort != AUTO or harness not in EFFORTS:
        return default
    return default


def resolved_for_either_harness_after_the_return(harness: str, lane: dict[str, str], default_model: str) -> ModelPick:
    # ruleid: lane-model-before-harness
    default = ModelPick(lane.get("model", ""), lane.get("effort", ""))
    if default.effort != AUTO or harness not in EFFORTS:
        return default
    if foreign(harness, default.model):
        default = replace(default, model=default_model)
    return default
