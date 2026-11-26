import os
from pathlib import Path
from typing import Any

from scripts.gates.base import Call, Decision, Who


def marker_path(session: str) -> Path:
    return Path(os.environ["AGENTIHOOKS_HOME"]) / "first-read" / session


def is_brief(command: str) -> bool:
    return False


def mark(payload: dict[str, Any]) -> None:
    pass


def reset(payload: dict[str, Any]) -> None:
    pass


class FirstRead:
    name = "first-read"
    default_mode = "observe"

    def matches(self, call: Call) -> bool:
        return False

    def decide(self, call: Call, who: Who, state: object) -> Decision:
        return Decision()
