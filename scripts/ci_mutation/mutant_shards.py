import json
from collections.abc import Iterable
from pathlib import Path


def shard_names(names: Iterable[str], shard: tuple[int, int]) -> set[str]:
    index, total = shard
    path = Path("shard-plan.json")
    if not path.is_file():
        return set(sorted(names)[index::total])
    plan = json.loads(path.read_text())
    if plan["shards"] != total:
        raise ValueError("measured mutation plan has a different shard count")
    names = set(names)
    if missing := names - plan["assignments"].keys():
        raise ValueError(f"measured mutation plan missing mutant names: {sorted(missing)}")
    return {name for name in names if holds(plan["assignments"][name], index)}


def function_name(name: str) -> str:
    return name.rpartition("__mutmut_")[0]


def unit_count(plan: dict) -> int:
    return sum(len(assignment) if isinstance(assignment, list) else 1 for assignment in plan["assignments"].values())


def holds(assignment: int | list[int], index: int) -> bool:
    return index in assignment if isinstance(assignment, list) else assignment == index


def split_names(plan: dict) -> set[str]:
    return {name for name, assignment in plan["assignments"].items() if isinstance(assignment, list)}


def unit_tests(plan: dict, name: str, index: int) -> set[str] | None:
    assignment = plan["assignments"].get(name)
    if not isinstance(assignment, list) or index not in assignment:
        return None
    split = plan["splits"][function_name(name)]
    return {*split["head"], *split["slices"][assignment.index(index)]}


def unit_cost(plan: dict, name: str, index: int) -> float:
    assignment = plan["assignments"][name]
    if not isinstance(assignment, list):
        return plan["costs"][name]
    return plan["splits"][function_name(name)]["costs"][assignment.index(index)]


def shard_tests(tests: list[str], name: str | None, index: int) -> list[str]:
    path = Path("shard-plan.json")
    unit = unit_tests(json.loads(path.read_text()), name, index) if name and path.is_file() else None
    return tests if unit is None else [test for test in tests if test in unit]
