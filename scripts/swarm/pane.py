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


INPUT_MARK = re.compile(r"\s*[❯›]\s?")
INPUT_RULE = re.compile(r"\s*─{3,}\s*$")
SGR = re.compile(r"\x1b\[([0-9;]*)m")
ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def typed_input(text: str) -> str:
    """The text typed into the agent's input line: the last line opening on an input marker, through its box's closing rule."""
    lines = _undimmed(text).splitlines()
    marks = [i for i, line in enumerate(lines) if INPUT_MARK.match(line)]
    if not marks:
        return ""
    box = lines[marks[-1] :]
    end = next((i for i, line in enumerate(box) if INPUT_RULE.match(line)), 1)
    box[0] = INPUT_MARK.sub("", box[0], count=1)
    return "\n".join(line.strip() for line in box[:end] if line.strip())


def _undimmed(text):
    """Placeholder hints render faint, so faint text is dropped and only its line breaks kept."""
    out, faint, at = [], False, 0
    for found in SGR.finditer(text):
        segment = text[at : found.start()]
        out.append("\n" * segment.count("\n") if faint else segment)
        faint = _faint_after(faint, found.group(1))
        at = found.end()
    out.append("\n" * text[at:].count("\n") if faint else text[at:])
    return ESCAPE.sub("", "".join(out))


def _faint_after(faint, params):
    codes = [int(code) if code else 0 for code in params.split(";")]
    i = 0
    while i < len(codes):
        if codes[i] in (38, 48, 58):
            i += 3 if codes[i + 1 : i + 2] == [5] else 5
            continue
        if codes[i] == 2:
            faint = True
        elif codes[i] in (0, 22):
            faint = False
        i += 1
    return faint
