import argparse
import json
import os
from pathlib import Path
from typing import Any

from scripts.ci_mutation.killers import KillMemory
from scripts.ci_mutation.measured import budget_overrun, plan_shards
from scripts.ci_mutation.mutant_shards import function_name
from scripts.ci_mutation.plan import SHARD_SETUP_SECONDS
from scripts.ci_mutation.probe import merge_probes, plan_fingerprint

SHARD_WORK_SECONDS = 360.0


def survivor_units(plan: dict[str, Any], name: str, memory: KillMemory | None) -> list[float]:
    """Price each unit of a mutant its head left alive by its slice alone, since the shard skips the head."""
    split = plan["splits"][function_name(name)]
    head = 1.25 * split.get("head_seconds", 0.0)
    units: list[float] = []
    for tests, cost in zip(split["slices"], split["costs"], strict=True):
        observed = memory.cost(name, tests) if memory is not None else None
        units.append(observed if observed is not None else cost - head)
    return units


def probed_plan(plan: dict[str, Any], budget: float, memory: KillMemory | None = None) -> dict[str, Any]:
    """Size the shards from what the early stage left, at most six minutes of work per worker on each shard."""
    results = plan["probe"]["results"]
    survivors = sorted(name for name, status in results.items() if status == "survived")
    killed = sorted(name for name, status in results.items() if status == "killed")
    unit_costs = {name: survivor_units(plan, name, memory) for name in survivors}
    costs = {name: cost for name, cost in plan["costs"].items() if name not in results}
    costs.update({name: sum(units) for name, units in unit_costs.items()})
    splits = {function_name(name): plan["splits"][function_name(name)] for name in survivors}
    remaining = min(budget - plan["probe"]["seconds"], SHARD_SETUP_SECONDS + SHARD_WORK_SECONDS)
    shards = plan_shards(
        costs, limit=min(10, len(plan["costs"])), splits=splits, unit_costs=unit_costs, budget=remaining
    )
    replanned = {
        **plan,
        **shards,
        "assignments": {
            **shards["assignments"],
            **{name: index % shards["shards"] for index, name in enumerate(killed)},
        },
        "costs": {**shards["costs"], **dict.fromkeys(killed, 0.0)},
        "unit_costs": unit_costs,
        "splits": plan["splits"],
    }
    return {**replanned, "probe": {**plan["probe"], "plan": plan_fingerprint(replanned)}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stats", type=Path, required=True)
    parser.add_argument("--budget", type=float, default=1080)
    args = parser.parse_args()
    path = args.stats / "plan.json"
    records = [json.loads(found.read_text()) for found in sorted(args.stats.glob("probe-*.json"))]
    merged = merge_probes(json.loads(path.read_text()), records)
    plan = probed_plan(merged, args.budget, KillMemory(args.stats / "killers"))
    path.write_text(json.dumps(plan))
    killed = sum(status == "killed" for status in plan["probe"]["results"].values())
    print(f"Early mutation kills: {killed} of {len(plan['probe']['results'])} split mutants")
    print(f"Probed mutation worker seconds: {plan['estimated_worker_seconds']:.1f}; probed shards: {plan['shards']}")
    if overrun := budget_overrun(plan, args.budget - plan["probe"]["seconds"]):
        print(f"::error title=Mutation plan over budget::{overrun}")
        return 1
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write(f"shards={json.dumps(list(range(plan['shards'])))}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
