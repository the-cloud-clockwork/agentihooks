import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PaneObservation:
    state: str
    prompt_title: str = ""


def selection_prompt(text: str) -> str:
    plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    lines = [line.strip() for line in plain.splitlines() if line.strip()]
    confirmations = [
        i for i, line in enumerate(lines) if re.search(r"enter\s+to\s+(?:confirm|select|continue)", line, re.I)
    ]
    if not confirmations:
        return ""
    if any(
        not re.match(r"(?:esc(?:ape)?\b|[↑↓←→]|use .*arrow)", line, re.I) for line in lines[confirmations[-1] + 1 :]
    ):
        return ""
    choices = [i for i, line in enumerate(lines) if re.match(r"[❯›→>]\s+\S", line)]
    if not choices:
        return ""
    titles = [line for line in lines[: choices[-1]] if line.endswith("?")]
    return titles[-1] if titles else ""
