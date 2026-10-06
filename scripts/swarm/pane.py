import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PaneObservation:
    state: str
    prompt_title: str = ""


def selection_prompt(text: str) -> str:
    plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    lines = [line.strip() for line in plain.splitlines() if line.strip()]
    if not lines or not re.search(r"enter\s+to\s+(?:confirm|select|continue)", lines[-1], re.I):
        return ""
    choices = [i for i, line in enumerate(lines) if re.match(r"[❯›→>]\s+\S", line)]
    if not choices:
        return ""
    titles = [line for line in lines[: choices[-1]] if line.endswith("?")]
    return titles[-1] if titles else ""
