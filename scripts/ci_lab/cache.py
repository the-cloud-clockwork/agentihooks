import os
from collections.abc import Callable, Mapping
from pathlib import Path

ROOT = "CI_LAB_CACHE_ROOT"
LIMIT = "CI_LAB_CACHE_LIMIT_BYTES"
POD_UID = "CI_LAB_CACHE_POD_UID"
SCAN_SECONDS = 10.0
_pid = os.getpid
_uid = os.getuid
_exit = os._exit


class PolicyError(ValueError):
    pass


def allocated_bytes(_root: Path) -> int:
    return 0


def run(
    start: Callable[[Mapping[str, str]], int],
    environ: Mapping[str, str],
    *,
    required: bool = False,
) -> int:
    del required
    return start(environ)
