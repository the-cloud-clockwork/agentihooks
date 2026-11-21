from collections.abc import Iterator
from typing import Any

DUMP_KEYS = "return {}"


def _batches(
    groups: list[tuple[list[str], dict[str, list[str]]]],
) -> Iterator[tuple[list[str], dict[str, list[str]]]]:
    yield from groups


def _dump_batch(redis: Any, keys: list[str], sets: dict[str, list[str]]) -> Any:
    # ok: snapshot-dump-unbatched-eval
    return redis.eval(DUMP_KEYS, len(keys) + len(sets), *keys, *sets, len(keys))


def _dump(redis: Any, groups: list[tuple[list[str], dict[str, list[str]]]]) -> list[Any]:
    rows: list[Any] = []
    for keys, sets in _batches(groups):
        # ok: snapshot-dump-unbatched-eval
        rows.append(_dump_batch(redis, keys, sets))
    return rows


def _dump_each(redis: Any, groups: list[tuple[list[str], dict[str, list[str]]]]) -> list[Any]:
    rows: list[Any] = []
    for batch in _batches(groups):
        # ok: snapshot-dump-unbatched-eval
        rows.append(_dump_batch(redis, *batch))
    return rows


def whole_dump(redis: Any, keys: list[str]) -> Any:
    # ruleid: snapshot-dump-unbatched-eval
    return redis.eval(DUMP_KEYS, len(keys), *keys, len(keys))


def registered_dump(redis: Any, keys: list[str]) -> Any:
    # ruleid: snapshot-dump-unbatched-eval
    script = redis.register_script(DUMP_KEYS)
    return script(keys=keys, args=[len(keys)])


def one_batch(redis: Any, keys: list[str]) -> Any:
    # ruleid: snapshot-dump-unbatched-eval
    return _dump_batch(redis, keys, {})


def unrelated(redis: Any, keys: list[str]) -> Any:
    # ok: snapshot-dump-unbatched-eval
    return redis.eval("return 1", len(keys), *keys)
