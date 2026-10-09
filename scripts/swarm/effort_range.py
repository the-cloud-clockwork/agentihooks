"""The effort range a swarm holds every lane agent's launch effort inside; both harnesses' levels share one rank."""

EFFORTS = {"claude": ("low", "medium", "high", "max"), "codex": ("low", "medium", "high", "xhigh")}
DEFAULT = ("medium", "high")
VARIABLE = "AGENTIHOOKS_SWARM_EFFORT_RANGE"


def rank(effort):
    for levels in EFFORTS.values():
        if effort in levels:
            return levels.index(effort)
    return None


def level(effort):
    found = rank(effort)
    return None if found is None else EFFORTS["claude"][found]


def named(harness, effort):
    found = rank(effort)
    return effort if found is None else EFFORTS[harness][found]


def of(config):
    return getattr(config, "effort_min", DEFAULT[0]), getattr(config, "effort_max", DEFAULT[1])


def clamp(harness, effort, bounds):
    found = rank(effort)
    if found is None:
        return effort
    low, high = (rank(edge) for edge in bounds)
    return EFFORTS[harness][min(max(found, low), high)]


def refusal(bounds, lanes):
    low, high = bounds
    if level(low) is None or level(high) is None:
        return f"effort-min and effort-max are one of {', '.join(EFFORTS['claude'])}, Codex xhigh standing for max"
    if rank(low) > rank(high):
        return f"effort-min {low} is above effort-max {high}"
    for name, lane in sorted(lanes.items()):
        found = rank(lane.get("effort"))
        if found is not None and not rank(low) <= found <= rank(high):
            return f"lane {name} effort {lane['effort']} is outside the swarm effort range {low} to {high}"
    return None


def parse(text):
    low, _, high = text.partition(":")
    return (low, high) if refusal((low, high), {}) is None else DEFAULT


def _without_effort(args):
    kept, skip = [], False
    for arg, after in zip(args, [*args[1:], ""]):
        if skip:
            skip = False
        elif arg == "--effort" or (arg == "-c" and after.startswith("model_reasoning_effort=")):
            skip = True
        elif not arg.startswith(("--effort=", "model_reasoning_effort=")):
            kept.append(arg)
    return kept


def launch_args(agent, args, environ):
    """A swarm lane launch's agent args carrying one effort flag, clamped into the swarm range; others unchanged."""
    if not environ.get("AGENTIHOOKS_SWARM_LANE"):
        return args
    from scripts.init_agent import model_effort, model_flags

    effort = clamp(agent, model_effort(agent, args, environ)[1], parse(environ.get(VARIABLE, "")))
    return [*_without_effort(args), *model_flags(agent, effort=effort)]
