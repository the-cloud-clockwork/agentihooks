from dataclasses import dataclass
from pathlib import Path

WORKER_RUN = "AGENTIHOOKS_WORKER_RUN"
WORKER_RUN_ID = "AGENTIHOOKS_WORKER_RUN_ID"


@dataclass(frozen=True)
class ProfileRun:
    profile: str
    harness: str
    input: str
    schema: dict | None
    tier: str
    effort: str
    timeout: float
    run_dir: Path


@dataclass(frozen=True)
class ProfileResult:
    status: str
    output: object
    transcript: Path
    session: str | None
    exit_code: int | None


def run_profile(run: ProfileRun) -> ProfileResult:
    raise NotImplementedError
