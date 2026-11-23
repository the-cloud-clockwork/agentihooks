from collections.abc import Mapping
from pathlib import Path

RUFF_CACHE = "RUFF_CACHE_DIR"


class Sealed:
    # ruleid: lab-static-step-env-without-ruff-cache
    @staticmethod
    def _sandbox(folder: Path, env: Mapping[str, str]) -> dict[str, str]:
        return {"PATH": "/bin", **env, "RUNNER_TEMP": str(folder)}


class Cached:
    # ok: lab-static-step-env-without-ruff-cache
    @staticmethod
    def _sandbox(folder: Path, env: Mapping[str, str]) -> dict[str, str]:
        return {"PATH": "/bin", RUFF_CACHE: str(folder / "ruff"), **env, "RUNNER_TEMP": str(folder)}
