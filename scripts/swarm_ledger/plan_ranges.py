import re
from urllib.parse import urlsplit

from scripts.swarm_ledger import ledger_artifacts

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
        if marker := _fence(line):
            run, info = marker
            if fence is None:
                fence = run
                continue
            if run.startswith(fence) and not info:
                fence = None
                continue
        if fence is None:
            heading = _heading(line)
            result.append((number, *heading) if heading else (number, 0, line))
    return result


def _fence(line: str) -> tuple[str, str] | None:
    stripped = line.lstrip()
    char = stripped[:1]
    if char not in ("`", "~"):
        return None
    run = stripped[: len(stripped) - len(stripped.lstrip(char))]
    return (run, stripped[len(run) :].strip()) if len(run) >= 3 else None


def _heading(line: str) -> tuple[int, str] | None:
    hashes = len(line) - len(line.lstrip("#"))
    rest = line[hashes:]
    if not 1 <= hashes <= 6 or not rest[:1].isspace():
        return None
    title = rest.strip()
    bare = title.rstrip("#")
    if bare != title and (not bare or bare[-1].isspace()):
        title = bare.rstrip()
    return hashes, title


def phase_lines(text: str, phases: list[dict]) -> dict[str, str]:
    entries = sections(text)
    end = len(text.splitlines())
    if len(phases) == 1 and end and not _headings(entries, phases[0]):
        return {phases[0]["id"]: f"1-{end}"}
    result = {}
    for phase in phases:
        matches = _headings(entries, phase)
        if len(matches) != 1:
            raise ValueError(f"plan needs one heading for phase {phase['title']}")
        start, level = matches[0]
        stop = next((n - 1 for n, depth, _ in entries if n > start and 0 < depth <= level), end)
        result[phase["id"]] = f"{start}-{stop}"
    return result


def _headings(entries: list[tuple[int, int, str]], phase: dict) -> list[tuple[int, int]]:
    return [(n, level) for n, level, title in entries if level and title.casefold() == phase["title"].casefold()]


def slice_lines(text: str, name: str, phase_range: str) -> str:
    lines = text.splitlines()
    start, end = bounds(phase_range)
    end = min(end, len(lines))
    entries = [(n, level, line) for n, level, line in sections(text) if start <= n <= end]
    matches = [n for n, _, line in entries if (match := ANCHOR.fullmatch(line)) and match[1] == name]
    if matches:
        if len(matches) != 1:
            raise ValueError(f"slice anchor {name} is missing or repeated in its phase")
        first = matches[0]
        body, level = _owner(entries, first)
    else:
        headings = [
            (n, depth)
            for n, depth, title in entries
            if depth and re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w.-])", title)
        ]
        if len(headings) != 1:
            raise ValueError(f"slice anchor {name} is missing or repeated in its phase")
        first, level = headings[0]
        body = first
    stop = next(
        (n for n, depth, line in entries if n > body and (ANCHOR.fullmatch(line) or 0 < depth <= level)), end + 1
    )
    last = stop - 1
    while not lines[last - 1].strip():
        last -= 1
    return f"{first}-{last}"


def _owner(entries: list[tuple[int, int, str]], anchor: int) -> tuple[int, int]:
    after = next(((n, depth) for n, depth, line in entries if n > anchor and (depth or line.strip())), None)
    if after and after[1]:
        return after
    headings = [depth for n, depth, _ in entries if n < anchor and depth]
    return anchor, headings[-1] if headings else 6


def stored_text(ref: dict, doc: dict) -> str:
    check_ref(ref)
    parts = urlsplit(ref["artifact"]).path.split("/")
    if len(parts) != 4 or parts[1] != "artifacts":
        raise ValueError("plan artifact must name a stored ledger artifact")
    _, _, slug, file_id = parts
    if not any(row.get("plan") is True and row["file"]["id"] == file_id for row in doc.get("artifacts", ())):
        raise ValueError("plan artifact is missing or is not marked as a plan")
    return ledger_artifacts.path_of(slug, file_id).read_bytes().decode()


def check_phase_ref(doc: dict, phase: dict) -> None:
    ref = phase["plan_ref"]
    if phase_lines(stored_text(ref, doc), [phase])[phase["id"]] != ref["lines"]:
        raise ValueError(f"plan_ref lines for phase {phase['id']} must be the range computed from its plan")


def task_slice(doc: dict, phase: dict, name: str, plan_url: str = "") -> str:
    ref = phase.get("plan_ref")
    if ref is not None:
        return slice_lines(stored_text(ref, doc), name, ref["lines"])
    url = plan_url or phase.get("plan_url")
    if not url:
        raise ValueError("publish a plan artifact for the phase before adding a plan slice")
    text = stored_text({"artifact": url, "lines": "1-1"}, doc)
    return slice_lines(text, name, f"1-{len(text.splitlines())}")


def invalid_tasks(plan: dict, doc: dict, ids: list[str]) -> list[str]:
    phase = next((p for p in doc["phases"] if p["id"] == plan["phase"]), {})
    return [task["id"] for task in doc["tasks"] if task["id"] in ids and not _ranged(doc, phase, task)]


def _ranged(doc: dict, phase: dict, task: dict) -> bool:
    try:
        return task.get("plan_lines") == task_slice(doc, phase, task["plan_slice"])
    except (KeyError, ValueError):
        return False
