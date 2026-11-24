from collections.abc import Mapping
from pathlib import Path

TYPE_WORKERS = "AGENTIHOOKS_TYPE_WORKERS"


# ruleid: lab-static-step-env-without-type-workers
def _sandbox(folder: Path, env: Mapping[str, str]) -> dict[str, str]:
    return {"PATH": "/bin", **env, "RUNNER_TEMP": str(folder)}


# ruleid: lab-static-step-env-without-type-workers
def _sandbox(folder: Path, env: Mapping[str, str]) -> dict[str, str]:
    """TYPE_WORKERS: the type gate sizes its own pool."""
    return {"PATH": "/bin", **env, "NOTE": "AGENTIHOOKS_TYPE_WORKERS: unset", "RUNNER_TEMP": str(folder)}


# ok: lab-static-step-env-without-type-workers
def _sandbox(folder: Path, env: Mapping[str, str]) -> dict[str, str]:
    return {"PATH": "/bin", TYPE_WORKERS: "1", **env, "RUNNER_TEMP": str(folder)}


# ok: lab-static-step-env-without-type-workers
def _sandbox(folder: Path, env: Mapping[str, str]) -> dict[str, str]:
    whitelist = {"PATH": "/bin", "AGENTIHOOKS_TYPE_WORKERS": "1", **env, "RUNNER_TEMP": str(folder)}
    return dict(whitelist)
