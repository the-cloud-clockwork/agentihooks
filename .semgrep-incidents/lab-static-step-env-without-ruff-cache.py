from collections.abc import Mapping
from pathlib import Path

RUFF_CACHE = "RUFF_CACHE_DIR"


# ruleid: lab-static-step-env-without-ruff-cache
def _sandbox(folder: Path, env: Mapping[str, str]) -> dict[str, str]:
    return {"PATH": "/bin", **env, "RUNNER_TEMP": str(folder)}


# ruleid: lab-static-step-env-without-ruff-cache
def _sandbox(folder: Path, env: Mapping[str, str]) -> dict[str, str]:
    """RUFF_CACHE: ruff keeps its cache in the head tree."""
    return {"PATH": "/bin", **env, "NOTE": "RUFF_CACHE_DIR: unset", "RUNNER_TEMP": str(folder)}


# ok: lab-static-step-env-without-ruff-cache
def _sandbox(folder: Path, env: Mapping[str, str]) -> dict[str, str]:
    return {"PATH": "/bin", RUFF_CACHE: str(folder / "ruff"), **env, "RUNNER_TEMP": str(folder)}


# ok: lab-static-step-env-without-ruff-cache
def _sandbox(folder: Path, env: Mapping[str, str]) -> dict[str, str]:
    whitelist = {"PATH": "/bin", "RUFF_CACHE_DIR": str(folder / "ruff"), **env, "RUNNER_TEMP": str(folder)}
    return dict(whitelist)
