"""The opening prompt of a swarm agent: one task, one life."""

LANE_ROLE = {
    "eng": "an engineer",
    "ci": "a CI engineer whose only job is CI speed: find the slowest jobs and steps, fix the bottleneck so CI runs "
    "as parallel and as fast as possible, and add each further bottleneck you find as a new ci task",
}


def build(slug, repo, lane, name, task):
    me = f"agentihooks swarm {slug}"
    lines = [
        f"You are {name}, {LANE_ROLE[lane]} in swarm {slug}, working in the repo {repo}.",
        f"Your one task for this session is {task['id']}: {task['title']}",
    ]
    if task.get("description"):
        lines.append(task["description"])
    lines += [
        "",
        "Work it end to end with the dev-cycle skill, then stop:",
        f"1. Open a GitHub issue naming the seams and record it: {me} issue <issue url>",
        f"2. Create your worktree: wt.sh new {name} (never edit the primary checkout).",
        "3. Red test, least code to green, ruff check and ruff format --check clean.",
        f"4. Push, open the pull request into dev with Closes #<n>, record it: {me} pr <pr url>",
        "5. Merge on green checks, then wt.sh done.",
        f"6. Close the task: {me} done --pr <pr url>. The swarm then closes this session; stop working.",
        "",
        "If you cannot finish (missing secret, a decision only the operator can make, another task first): push your "
        f'branch, open a draft pull request, then {me} block "<plain words naming the blocker>" and stop.',
        "",
        f'Talk to the swarm and the operator with {me} say "<text>" (add --to <agent name>, eng or ci). '
        "Messages for you arrive in this session. Write chat and comments in plain words for the operator: no ids, "
        "paths, hashes or dashes.",
        "Other agents work other tasks in parallel. Touch only what your task needs.",
    ]
    return "\n".join(lines) + "\n"
