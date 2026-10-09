from collections.abc import Iterable


def shard_names(names: Iterable[str], shard: tuple[int, int]) -> set[str]:
    index, total = shard
    return set(sorted(names)[index::total])
