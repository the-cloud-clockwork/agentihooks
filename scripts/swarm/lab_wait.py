"""Lab waits: a wait on a check run, such as a lab or lab shadow gate's, ends when GitHub reports it completed, and a
wait on a lab grade url, which the lab remote prints for each pushed grade ref, ends when the lab closes its grades."""

import json
import re
from typing import Any
from urllib.request import urlopen

from scripts.swarm import run_wait

CHECK_URL = re.compile(
    r"https://github\.com/([\w.-]+/[\w.-]+)/(?:runs/([0-9]+)|pull/[0-9]+/checks\?check_run_id=([0-9]+))"
)
GRADE_URL = re.compile(r"https?://[^\s?#]+/grades/[0-9a-f]{40}(?:[0-9a-f]{24})?")
TIMEOUT_S = 3
PASSED = ("success", "neutral", "skipped")


def grades(url: str) -> list[dict[str, Any]]:
    """Every grade the lab serves at a grade url, as its GET /grades/<sha> route answers."""
    with urlopen(url, timeout=TIMEOUT_S) as reply:
        doc: Any = json.loads(reply.read())
    found: Any = doc.get("grades") if isinstance(doc, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not isinstance(found, list):
        raise ValueError("lab grades document carries no grades")
    return found  # pyright: ignore[reportUnknownVariableType]


def grade_resolution(target: str) -> str:
    """What ended the wait on the lab grade at target, in plain words, or '' while a live grade of its head is open."""
    outcome = f"lab grade {target}"
    try:
        found = grades(target)
    except (OSError, ValueError):
        return ""
    live = [grade for grade in found if not grade.get("superseded")]
    if found and not live:
        return f"{outcome}, superseded; a newer head replaced it"
    if not live or any(grade.get("conclusion") is None for grade in live):
        return ""
    if all(grade.get("conclusion") == "success" for grade in live):
        return f"{outcome}, now green"
    failed: set[str] = set()
    for grade in live:
        checks: dict[str, dict[str, Any]] = grade.get("checks") or {}
        failed.update(name for name, check in checks.items() if check.get("conclusion") not in PASSED)
    return f"{outcome}, now red; failed {', '.join(sorted(failed)) or 'with no failed check named'}"


def resolution(target: str) -> str:
    """What ended the wait on the check run or lab grade at target, in plain words, or '' while it still holds."""
    if GRADE_URL.fullmatch(target):
        return grade_resolution(target)
    outcome = f"lab check {target}"
    found = CHECK_URL.fullmatch(target)
    if found is None:
        return f"{outcome}, not a check run url; wait on a check run url"
    repo, plain, pulled = found.groups()
    try:
        check = run_wait.read(f"repos/{repo}/check-runs/{plain or pulled}", "waits.lab")
    except run_wait.Unreachable as error:
        return f"{outcome}, missing; GitHub has no such check run" if "HTTP 404" in str(error) else ""
    if check.get("status") != "completed":
        return ""
    if check.get("conclusion") == "success":
        return f"{outcome}, now green"
    gate = str(check.get("name")).split("/", 1)[-1]
    return f"{outcome}, now red; gate {gate} concluded {check.get('conclusion')}"
