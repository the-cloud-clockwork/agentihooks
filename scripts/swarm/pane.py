import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PaneObservation:
    state: str
    prompt_title: str = ""


def selection_prompt(text: str) -> str:
    plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    raw = [line.strip() for line in plain.splitlines()]
    lines = [line for line in raw if line]
    confirmations = [
        i for i, line in enumerate(lines) if re.search(r"enter\s+(?:to\s+)?(?:confirm|select|continue)", line, re.I)
    ]
    if not confirmations:
        return ""
    if any(
        not re.match(r"(?:esc(?:ape)?\b|[↑↓←→]|use .*arrow)", line, re.I) for line in lines[confirmations[-1] + 1 :]
    ):
        return ""
    choices = [i for i, line in enumerate(lines) if CHOICE.match(line)]
    if not choices:
        return ""
    questions = [line[: line.rindex("?") + 1] for line in lines[: choices[-1]] if "?" in line]
    return questions[-1] if questions else _heading(raw)


CHOICE = re.compile(r"[❯›→>]\s+\S")


def _heading(raw):
    """A prompt without a question is titled by the first line of the paragraph just above its choices."""
    above = raw[: next(i for i, line in enumerate(raw) if CHOICE.match(line))]
    while above and not above[-1]:
        above.pop()
    start = max((i + 1 for i, line in enumerate(above) if not line), default=0)
    return above[start] if above else ""


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
    parts = SGR.split(text)
    out, faint = [parts[0]], False
    for params, segment in zip(parts[1::2], parts[2::2]):
        faint = _faint_after(faint, params)
        out.append("\n" * segment.count("\n") if faint else segment)
    return ESCAPE.sub("", "".join(out))


def _faint_after(faint, params):
    codes = iter([int(code) if code else 0 for code in params.split(";")])
    for code in codes:
        if code in (38, 48, 58):
            for _ in range(1 if next(codes, None) == 5 else 3):
                next(codes, None)
            continue
        faint = (faint or code == 2) and code not in (0, 22)
    return faint
