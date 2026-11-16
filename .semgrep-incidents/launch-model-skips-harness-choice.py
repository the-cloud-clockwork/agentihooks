from dataclasses import replace
from typing import Any

from scripts.swarm import model_pick
from scripts.swarm.model_pick import ModelPick, foreign, suited


def lane_model(chosen: dict[str, Any]) -> str:
    return chosen.get("model") or ""


def hive_order_assignment(agent: Any, config: Any, lane: str, harness: str) -> dict[str, Any]:
    fallback = getattr(config, f"{harness}_model")
    # ruleid: launch-model-skips-harness-choice
    return {"harness": harness, "model": agent.model or config.lanes[lane].get("model") or fallback}


def successor_launch(saved: dict[str, Any], chosen: dict[str, Any]) -> dict[str, Any]:
    # ruleid: launch-model-skips-harness-choice
    return {**saved, "model": saved.get("model") or lane_model(chosen)}


def lane_default(chosen: dict[str, Any], model: str) -> ModelPick:
    # ruleid: launch-model-skips-harness-choice
    return ModelPick(chosen.get("model") or model, chosen.get("effort"))


def qualified_lane_default(chosen: dict[str, Any], model: str) -> model_pick.ModelPick:
    # ruleid: launch-model-skips-harness-choice
    return model_pick.ModelPick(chosen.get("model", "") or model, chosen.get("effort"))


def replaced_pick(picked: ModelPick, chosen: dict[str, Any]) -> ModelPick:
    # ruleid: launch-model-skips-harness-choice
    return replace(picked, model=chosen.get("model"))


def checked_by_harness_name_only(harness: str, lane: dict[str, str], fallback: str) -> ModelPick:
    # ruleid: launch-model-skips-harness-choice
    default = ModelPick(lane.get("model", ""), lane.get("effort", ""))
    if harness == "codex":
        default = replace(default, model=fallback)
    return default


def suited_hive_order_assignment(agent: Any, config: Any, lane: str, harness: str) -> dict[str, Any]:
    fallback = getattr(config, f"{harness}_model")
    # ok: launch-model-skips-harness-choice
    return {
        "harness": harness,
        "model": suited(harness, agent.model or config.lanes[lane].get("model") or fallback, fallback),
    }


def suited_successor_launch(
    saved: dict[str, Any], chosen: dict[str, Any], harness: str, fallback: str
) -> dict[str, Any]:
    # ok: launch-model-skips-harness-choice
    return {**saved, "model": saved.get("model") or model_pick.suited(harness, lane_model(chosen), fallback)}


def suited_lane_default(chosen: dict[str, Any], harness: str, model: str) -> model_pick.ModelPick:
    # ok: launch-model-skips-harness-choice
    return model_pick.ModelPick(model_pick.suited(harness, chosen.get("model") or model, model), chosen.get("effort"))


def resolved_pick(harness: str, lane: dict[str, str], fallback: str) -> ModelPick:
    # ok: launch-model-skips-harness-choice
    default = ModelPick(lane.get("model", ""), lane.get("effort", ""))
    if foreign(harness, default.model):
        default = replace(default, model=fallback)
    return default
