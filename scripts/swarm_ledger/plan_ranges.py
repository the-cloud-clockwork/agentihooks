import re
from urllib.parse import urlsplit

from scripts.swarm_ledger import ledger_artifacts

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
ANCHOR = re.compile(r"^\s*<!--\s*slice:\s*([\w.-]+)\s*-->\s*$")
LINES = re.compile(r"([1-9][0-9]*)-([1-9][0-9]*)")


def bounds(value: str) -> tuple[int, int]:
    match = LINES.fullmatch(value) if isinstance(value, str) else None
    if match is None or int(match[1]) > int(match[2]):
        raise ValueError("plan lines must be an ordered inclusive range")
    return int(match[1]), int(match[2])


def check_ref(ref: dict) -> None:
    if not isinstance(ref, dict) or set(ref) != {"artifact", "lines"}:
        raise ValueError("plan_ref needs artifact and lines")
    address = urlsplit(ref["artifact"]) if isinstance(ref["artifact"], str) else None
    if address is None or address.scheme not in ("http", "https") or not address.netloc:
        raise ValueError("plan artifact must be an http or https link")
    bounds(ref["lines"])


def sections(text: str) -> list[tuple[int, int, str]]:
    result = []
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence is None:
            result.append(
                (number, len(match[1]), match[2]) if (match := HEADING.fullmatch(line)) else (number, 0, line)
            )
    return result


def phase_lines(text: str, phases: list[dict]) -> dict[str, str]:
    entries = sections(text)
    end = len(text.splitlines())
    result = {}
    for phase in phases:
        matches = [(n, level) for n, level, title in entries if level and title.casefold() == phase["title"].casefold()]
        if not matches and len(phases) == 1 and end:
            result[phase["id"]] = f"1-{end}"
            continue
        if len(matches) != 1:
            raise ValueError(f"plan needs one heading for phase {phase['title']}")
        start, level = matches[0]
        stop = next((n - 1 for n, depth, _ in entries if n > start and 0 < depth <= level), end)
        result[phase["id"]] = f"{start}-{stop}"
    return result


def slice_lines(text: str, name: str, phase_range: str) -> str:
    start, end = bounds(phase_range)
    entries = [(n, level, line) for n, level, line in sections(text) if start <= n <= end]
    matches = [n for n, _, line in entries if (match := ANCHOR.fullmatch(line)) and match[1] == name]
    if len(matches) != 1:
        raise ValueError(f"slice anchor {name} is missing or repeated in its phase")
    first, body, level = _owner(entries, matches[0], start)
    stop = next(
        (n for n, depth, line in entries if n > body and (ANCHOR.fullmatch(line) or 0 < depth <= level)), end + 1
    )
    lines = text.splitlines()
    last = next(n for n in range(stop - 1, body - 1, -1) if n == body or lines[n - 1].strip())
    return f"{first}-{last}"


def _owner(entries: list[tuple[int, int, str]], anchor: int, start: int) -> tuple[int, int, int]:
    after = next(((n, depth) for n, depth, line in entries if n > anchor and (depth or line.strip())), None)
    if after and after[1]:
        return anchor, after[0], after[1]
    before = [(n, depth) for n, depth, line in entries if n < anchor and (depth or line.strip())]
    if before and before[-1][1] and before[-1][0] > start:
        return before[-1][0], anchor, before[-1][1]
    headings = [depth for n, depth, _ in entries if n < anchor and depth]
    return anchor, anchor, headings[-1] if headings else 6


def stored_text(ref: dict, doc: dict) -> str:
    check_ref(ref)
    parts = urlsplit(ref["artifact"]).path.strip("/").split("/")
    if len(parts) != 3 or parts[0] != "artifacts":
        raise ValueError("plan artifact must name a stored ledger artifact")
    _, slug, file_id = parts
    if not any(row.get("plan") is True and row["file"]["id"] == file_id for row in doc.get("artifacts", [])):
        raise ValueError("plan artifact is missing or is not marked as a plan")
    return ledger_artifacts.path_of(slug, file_id).read_text(encoding="utf-8")


def task_slice(doc: dict, phase: dict, name: str) -> str:
    ref = phase.get("plan_ref")
    if ref is None:
        raise ValueError("publish a plan artifact for the phase before adding a plan slice")
    return slice_lines(stored_text(ref, doc), name, ref["lines"])


def invalid_tasks(plan: dict, doc: dict, ids: list[str]) -> list[str]:
    bad = []
    for task in doc["tasks"]:
        if task["id"] not in ids:
            continue
        phase = next(p for p in doc["phases"] if p["id"] == plan["phase"])
        try:
            expected = task_slice(doc, phase, task.get("plan_slice", task["id"]))
        except ValueError:
            bad.append(task["id"])
            continue
        if task.get("plan_lines") != expected:
            bad.append(task["id"])
    return bad
