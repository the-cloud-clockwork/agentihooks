import subprocess
from dataclasses import dataclass

SEALED = "sealed"
READ_ONLY = "read-only"
TIMEOUT_S = 900
KEPT = ("HOME", "PATH")


class HeadlessError(RuntimeError):
    pass


@dataclass(frozen=True)
class Request:
    clamp: str
    profile_prompt: str
    user_input: str
    schema: dict
    model: str
    effort: str


def execute(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    try:
        done = subprocess.run(
            command, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise HeadlessError(f"{command[0]} ran past {TIMEOUT_S} seconds") from exc
    if done.returncode != 0:
        raise HeadlessError(f"{command[0]} exited {done.returncode}")
    return done
