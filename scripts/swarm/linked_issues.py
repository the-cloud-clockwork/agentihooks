"""Close the issues a merged ready pull request settles: GitHub closes them only on a merge into the default branch."""

import re

from scripts.swarm import stack
from scripts.tick.adapters.github import command
from scripts.tick.deadline import Deadline

KEYWORD = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+#(\d+)\b", re.IGNORECASE)
COMMENT = "Closed by the swarm: pull request {url} merged into dev."


def named(url):
    done = stack.shell(command(["pr", "view", url, "--json", "body", "--jq", ".body"]))
    if done.returncode != 0:
        return [], [f"could not read the issues named by {url}: {done.stderr.strip()}"]
    repo = url.split("/pull/")[0]
    return [f"{repo}/issues/{number}" for number in KEYWORD.findall(done.stdout)], []


def settle(url, finished, rows):
    recorded = [rows[task]["issue_url"] for task in finished if rows[task].get("issue_url")]
    held = {row.get("issue_url") for task, row in rows.items() if task not in finished and row.get("state") != "done"}
    issues, actions = named(url)
    for issue in dict.fromkeys([*recorded, *issues]):
        if issue not in held:
            actions += _close(issue, url)
    return actions


def _close(issue: str, url: str) -> list[str]:
    """Close an open issue through the source control port and say so on it, naming any step that failed."""
    try:
        provider, reference = stack.source_control(issue)
        if provider.issue_state(reference, deadline=Deadline.after(stack.PORT_SECONDS)) != "open":
            return []
        provider.close_issue(reference, deadline=Deadline.after(stack.PORT_SECONDS))
    except stack.PORT_ERRORS as exc:
        return [f"could not close issue {issue}: {stack.refusal(exc)}"]
    try:
        provider.comment(reference, COMMENT.format(url=url), deadline=Deadline.after(stack.PORT_SECONDS))
    except stack.PORT_ERRORS as exc:
        return [f"closed issue {issue}; could not comment on it: {stack.refusal(exc)}"]
    return [f"closed issue {issue}"]
