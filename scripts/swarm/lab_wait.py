"""Lab check waits: a wait on a check run, such as a lab or lab shadow gate's, ends when GitHub reports it completed."""

import re

from scripts.swarm import run_wait

CHECK_URL = re.compile(
    r"https://github\.com/([\w.-]+/[\w.-]+)/(?:runs/([0-9]+)|pull/[0-9]+/checks\?check_run_id=([0-9]+))"
)


def resolution(target: str) -> str:
    """What ended the wait on the check run at target, in plain words, or '' while it still holds."""
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
