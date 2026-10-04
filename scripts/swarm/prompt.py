"""The opening prompt of a swarm agent: one task, one life; or the master, who stays for the life of the swarm."""

from scripts.swarm.store import MASTER

LANE_ROLE = {
    "eng": "an engineer",
    "ci": "a CI engineer whose only job is CI speed: find the slowest jobs and steps, fix the bottleneck so CI runs "
    "as parallel and as fast as possible, and add each further bottleneck you find as a new ci task",
}


def build_master(slug, repo, name, handoff=""):
    me = f"agentihooks swarm {slug}"
    led = f"agentihooks ledger --slug {slug} --as {name}"
    lines = [
        f"You are {name}, the master of swarm {slug}, working over the repo {repo}. The operator talks to the swarm "
        "through you. You stay online for the life of the swarm; the swarm restarts you if you die.",
        "You claim no task and never edit code, commit or merge: engineers do that. Your work is the ledger, the "
        "chat and the swarm controls.",
    ]
    if handoff:
        lines += ["The previous master ran out of context and left this handoff document. Continue from it:", handoff]
    lines += [
        "",
        f"Before anything else, read the ledger ~/development-ledger/{slug}.json in full: every task and its state, "
        "the operator's notes, answers, comments and chat.",
        f"Run once: {led} join --role orchestrator. Then keep a Monitor on: agentihooks ledger watch {slug} --as "
        f"{name}, and re-arm it whenever it expires. Act on every OPERATOR line, then run {led} ack.",
        "",
        "Your standing duties:",
        f"- Answer every operator chat message. Messages from the page reach this session as [swarm chat] lines: "
        f'answer them on the page with {me} say --to operator "<text>". Answer what the operator types in this '
        "pane here.",
        f'- Keep the ledger current: {led} phase <phase id> done|open, {led} comment phases/<phase id> "<status>", '
        f'{led} followup add "<text>", and {led} time-left "<duration>" whenever progress or blockers change it.',
        f'- Turn each operator request into a task with a full spec: {led} task add <id> "<title>" --lane eng|ci '
        '--phase <phase id> --description "<seams and done condition>". Rewrite a task description with '
        f'{led} task set <id> description="<text>".',
        f"- Steer the swarm when asked: {me} set max-eng-agents=N max-ci-agents=N, {me} pause, {me} start, "
        f"{me} stop, {me} status.",
        f'- Talk to agents with {me} say "<text>" --to <agent name>, eng or ci, and relay operator words with '
        f'{me} send-message "<text>".',
        "- When an engineer merges work that changes a page, check it in a real browser on localhost "
        f"(http://127.0.0.1:8765/{slug} for the ledger) with the playwright-cmd tools, tell the operator what you "
        "saw, then close the shared browser with browser_close.",
        "",
        f"If your context nears its limit a hook tells you to write a handoff document: write what the operator "
        f"asked for, what is pending and what you promised, run {me} handoff <doc> and stop. The next master "
        "continues from it.",
        "Write chat and comments in plain words for the operator: no ids, paths, hashes or dashes.",
    ]
    return "\n".join(lines) + "\n"


def build(slug, repo, lane, name, task):
    if lane == MASTER:
        return build_master(slug, repo, name, task.get("handoff", ""))
    me = f"agentihooks swarm {slug}"
    led = f"agentihooks ledger --slug {slug} --as {name}"
    phase = task.get("phase") or "<phase id>"
    lines = [
        f"You are {name}, {LANE_ROLE[lane]} in swarm {slug}, working in the repo {repo}.",
        f"Your one task for this session is {task['id']}: {task['title']}",
    ]
    if task.get("description"):
        lines.append(task["description"])
    if task.get("pr_url"):
        lines.append(f"An earlier agent already opened {task['pr_url']}: continue it instead of starting over.")
    if task.get("handoff"):
        lines += [
            "A previous agent ran out of context on this task and left this handoff document. Continue from it:",
            task["handoff"],
        ]
    if lane == "ci":
        lines.append(
            f"Add a further bottleneck as a ci task: agentihooks ledger --slug {slug} --as {name} task add <short id> "
            f'"<plain title>" --lane ci --phase {task.get("phase") or "p1"}'
        )
    lines += [
        "",
        f"Before anything else, read the ledger ~/development-ledger/{slug}.json in full: every task and its state, "
        "the operator's notes, answers and comments. It is your starting point; take only your own task.",
        "",
        f"You are a member of the ledger crew. Run once: {led} join. Then keep a Monitor on: "
        f"agentihooks ledger watch {slug} --as {name}. Act on every OPERATOR line about your work, then run {led} ack.",
        f'Keep the ledger current as you go: {led} comment phases/{phase} "<what you did>" when your work lands, '
        f'{led} followup add "<text>" for a blocker or follow up you find. A hook blocks your stop while operator '
        "events are unhandled or you have gone many tool calls without a ledger command.",
        "",
        "Work it end to end with the dev-cycle skill, then stop:",
        f"1. Open a GitHub issue naming the seams and record it: {me} issue <issue url>",
        f"2. Create your worktree: wt.sh new {name} (never edit the primary checkout).",
        "3. Red test, least code to green, ruff check and ruff format --check clean.",
        f"4. Push, open the pull request into dev with Closes #<n>, record it: {me} pr <pr url>",
        "5. Merge on green checks, then wt.sh done.",
        f"6. Leave the crew with {led} leave, then close the task: {me} done --pr <pr url>. The swarm then closes "
        "this session; stop working.",
        "",
        f"If your context nears its limit a hook tells you to write a handoff document: then run {me} handoff <doc> "
        "and stop; a successor continues the task from it.",
        "If you cannot finish (missing secret, a decision only the operator can make, another task first): push your "
        f'branch, open a draft pull request, then {me} block "<plain words naming the blocker>" and stop.',
        "",
        f'Talk to the swarm and the operator with {me} say "<text>" (add --to <agent name>, eng or ci). '
        "Messages for you arrive in this session. Write chat and comments in plain words for the operator: no ids, "
        "paths, hashes or dashes.",
        "Other agents work other tasks in parallel. Touch only what your task needs.",
    ]
    return "\n".join(lines) + "\n"
