from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from mutmut.mutation.data import SourceFileMutationData

from scripts.ci_mutation.killers import KillMemory
from scripts.ci_mutation.mutant_shards import shard_tests


@contextmanager
def retry_timeouts(
    engine: Any,
    memory: KillMemory,
    index: int,
    run_tests: Callable[..., int],
    run_unit: Callable[..., int],
) -> Iterator[Callable[[int, int], int | None]]:
    register_pid = SourceFileMutationData.register_pid
    records: dict[int, tuple[str, str | None]] = {}

    def remember(data: SourceFileMutationData, *, pid: int, key: str) -> None:
        records[pid] = (key, memory.killer(key))
        register_pid(data, pid=pid, key=key)

    def retry(pid: int, status: int) -> int | None:
        name, killer = records.pop(pid)
        if engine.status_by_exit_code.get(engine.os.waitstatus_to_exitcode(status)) != "timeout" or killer is None:
            return None
        function = engine.mangled_name_from_mutant_name(name)
        tests = sorted(
            engine.mutmut.tests_by_mangled_function_name[function],
            key=lambda test: engine.mutmut.duration_by_test[test],
        )
        chosen = shard_tests(tests, name, index)
        if killer not in chosen:
            return None
        chosen = [test for test in chosen if test != killer]
        replacement = engine.os.fork()
        if replacement == 0:
            engine.os.environ["MUTANT_UNDER_TEST"] = name
            result = run_unit(run_tests, engine.PytestRunner(), name, tests, chosen, memory, engine) if chosen else 33
            engine.os._exit(result)
        config = engine.Config.get()
        timeout = sum(engine.mutmut.duration_by_test[test] for test in tests) + config.timeout_constant
        engine.register_timeout(pid=replacement, timeout_s=timeout * config.timeout_multiplier)
        return replacement

    SourceFileMutationData.register_pid = remember
    try:
        yield retry
    finally:
        SourceFileMutationData.register_pid = register_pid
        engine.os.stop_retries()
