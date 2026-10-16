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
    return {name for name in names if plan["assignments"][name] == index}
