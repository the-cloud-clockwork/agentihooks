"""Close the issues a merged ready pull request settles: GitHub closes them only on a merge into the default branch."""

import re

from scripts.swarm import stack

KEYWORD = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+#(\d+)\b", re.IGNORECASE)
COMMENT = "Closed by the swarm: pull request {url} merged into dev."


def named(url):
    done = stack.shell(["gh", "pr", "view", url, "--json", "body", "--jq", ".body"])
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


def _close(issue, url):
    state = stack.shell(["gh", "issue", "view", issue, "--json", "state", "--jq", ".state"])
    if state.returncode != 0:
        return [f"could not close issue {issue}: {state.stderr.strip()}"]
    if state.stdout.strip() != "OPEN":
        return []
    closed = stack.shell(["gh", "issue", "close", issue])
    if closed.returncode != 0:
        return [f"could not close issue {issue}: {closed.stderr.strip()}"]
    said = stack.shell(["gh", "issue", "comment", issue, "--body", COMMENT.format(url=url)])
    if said.returncode != 0:
        return [f"closed issue {issue}; could not comment on it: {said.stderr.strip()}"]
    return [f"closed issue {issue}"]
