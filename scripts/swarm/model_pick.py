from dataclasses import dataclass, replace

from hooks.classifier import ClassifierUnavailable, decide, definitions, runner
from scripts.swarm.effort_range import DEFAULT as DEFAULT_RANGE
from scripts.swarm.effort_range import EFFORTS, clamp

PURPOSE = "model-pick"
QUESTION = "option"
AUTO = "auto"
BAND = "band"
SONNET = "sonnet"
SONNET_EFFORT = "xhigh"
FRONTIER_EFFORT = "high"
KEYS = ("lower", "higher")
STEPS = ((SONNET, SONNET_EFFORT), ("", "medium"), ("", "high"), ("", "xhigh"))
STARTS = {"S": 0, "M": 1, "L": 2}
SHIFTS = {"urgent": 1, "high": 1, "low": -1}
LABELS = {SONNET: "Sonnet", "opus": "Opus", "gpt-6.1-sol": "Sol"}
EFFORT_WORDS = {"medium": "medium effort", "high": "high effort", "xhigh": "extra high effort"}
FITS = {
    SONNET: "a small change the task fully specifies, with no design choice left",
    "medium": "the task names what to change and how to check it",
    "high": "several seams, or a cause to find inside one component",
    "xhigh": "an unknown cause across several components, or a redesign of a core concept",
}


@dataclass(frozen=True)
class ModelPick:
    model: str
    effort: str
    source: str = "lane-default"
    confidence: float | None = None


def frontier(harness: str, default_model: str = "") -> ModelPick:
    from scripts.init_agent import MODEL_DEFAULTS

    return ModelPick(default_model or MODEL_DEFAULTS[harness], FRONTIER_EFFORT, "frontier")


def band(harness: str, difficulty: str | None, rank: str | None) -> tuple[ModelPick, ...]:
    """The two neighbouring run options a task's size and rank allow, cheapest first; Codex has no Sonnet step."""
    from scripts.init_agent import MODEL_DEFAULTS

    start = min(max(STARTS.get(difficulty, STARTS["M"]) + SHIFTS.get(rank, 0), 0), len(STEPS) - 2)
    steps = [step for step in STEPS[start : start + 2] if harness != "codex" or step[0] != SONNET]
    return tuple(ModelPick(model or MODEL_DEFAULTS[harness], effort, BAND) for model, effort in steps)


def floor(options: tuple[ModelPick, ...]) -> ModelPick:
    return next(option for option in options if option.model != SONNET)


def described(option: ModelPick) -> str:
    fit = FITS[SONNET if option.model == SONNET else option.effort]
    return f"{LABELS.get(option.model, option.model)} at {EFFORT_WORDS[option.effort]}: {fit}"


def pick(
    harness: str, lane: dict, task: dict, environ: dict, bounds: tuple = DEFAULT_RANGE, default_model: str = ""
) -> ModelPick:
    from scripts.init_agent import model_effort

    default = ModelPick(lane.get("model", ""), lane.get("effort", ""))
    if default.effort != AUTO or harness not in EFFORTS:
        return default
    difficulty = task.get("difficulty") if task.get("difficulty") in STARTS else "M"
    rank = task.get("rank") or "normal"
    fixed = default.model not in ("", AUTO)
    model = default.model if fixed else default_model or model_effort(harness, [], environ)[0]
    if model == SONNET:
        return ModelPick(SONNET, SONNET_EFFORT, BAND)
    sonnet = not fixed and clamp(harness, SONNET_EFFORT, bounds) == SONNET_EFFORT
    offered = [option for option in band(harness, difficulty, rank) if sonnet or option.model != SONNET]
    options = tuple(option if option.model == SONNET else replace(option, model=model) for option in offered)
    chosen = options[offered.index(floor(offered))]
    if len(options) == 1:
        return chosen
    try:
        output = runner.run(
            PURPOSE,
            {
                "title": task.get("title", ""),
                "description": task.get("description", ""),
                "kind": task.get("kind", "code"),
                "territory_size": len(task.get("territory", [])),
                "difficulty": difficulty,
                "rank": rank,
            },
            {"difficulty": difficulty, "rank": rank, **{key: described(o) for key, o in zip(KEYS, options)}},
            harness,
            decider=decide,
            environ=environ,
        )
    except (ClassifierUnavailable, definitions.DefinitionError):
        return chosen
    verdict = output.verdicts[QUESTION]
    if verdict in KEYS:
        chosen = options[KEYS.index(verdict)]
    return replace(chosen, source=output.raw.source, confidence=output.raw.answers[QUESTION].confidence)
