"""The opening prompt of a swarm agent: one task, one life; or the master, who stays for the life of the swarm."""

import json
import os

from scripts.doctor import priming
from scripts.handoff.check import section
from scripts.inbox.seats import MATURITIES
from scripts.swarm import naming, plan_review
from scripts.swarm.health.verdicts import VERDICTS
from scripts.swarm.store import ASSIST, DELEGATE, DISPATCH, FULL, MANUAL, MASTER
from scripts.swarm_ledger import ledger_close, ledger_kinds, plan_read

CLOSES = "The swarm then closes this session; stop working."
OVERLAP_LINE = (
    "Running tasks share areas with yours. Coordinate with each claimant through the inbox "
    '(agentihooks msg send <claimant> "<text>") and merge dev into your branch before your own merge:'
)
GROUP_LINE = (
    "Your task leads a group: deliver these grouped tasks too, in the same worktree and one pull request, and name "
    "each in its body. When it merges, swarm done on your task closes them all:"
)
PUBLISHED = "It opens a GitHub issue where the repo has issues, else a ledger artifact"
THROUGH_CODE = (
    "Reach that state through code: any change to what runs goes through a worktree (wt.sh new {name}), a pull "
    "request into dev and CI, never a live patch."
)
SERENA = (
    "mcp__serena__activate_project with its absolute path. Python is read and edited through Serena "
    "(find_symbol, replace_symbol_body, insert_after_symbol, replace_content); built-in Edit and shell "
    "rewrites of existing .py files are blocked."
)
STACKED_KINDS = ("code", "ci")
RESTACK = (
    " Then run {me} restack in it: it rebases the parked work onto dev. On a conflict it lists the files: resolve "
    "them, run git rebase --continue, then run {me} restack again."
)
CONTRACT_LABELS = (("must", "Must be true"), ("check", "Checked by"), ("judge", "Judged by"))
OLDER_RECAPS = 3
INBOX_LINE = (
    "Operator writes on the ledger reach you as inbox messages at your next tool call, and the swarm wakes you with a "
    "prompt when you sit idle."
)

LANE_ROLE = {
    "plan": "a planner whose task is to slice its phase into tasks for review",
    "eng": "an engineer",
    "ci": "a CI engineer whose only job is CI speed: find the slowest jobs and steps, fix the bottleneck so CI runs "
    "as parallel and as fast as possible, and propose each further bottleneck you find as a follow up",
}


def ledger_read(slug: str) -> str:
    return f"agentihooks ledger --slug {slug} show"


def build_master(slug, repo, name, task, autonomy=DELEGATE):
    from scripts.profiles import codex_master

    codex = task.get("harness") == "codex"
    if codex:
        task = codex_master.handoff(task, slug)
    me = f"agentihooks swarm {slug} --as {name}"
    led = f"agentihooks ledger --slug {slug} --as {name}"
    summary = summary_lines(slug)
    lines = [
        f"You are {name}, the master of swarm {slug}, working over the repo {repo}. The operator talks to the swarm "
        "through you. You stay online for the life of the swarm; the swarm restarts you if you die.",
        "You troubleshoot with read only diagnostics, plan with the operator and configure the swarm, the ledger "
        "and the operator's environment with him through the agentihooks commands and tools. You never edit code or config "
        "files in a repository, commit, merge or claim a task: engineers do that.",
    ]
    lines += [
        *priming_lines(task),
        *summary,
        "",
        f"Before anything else, read the ledger in full with {ledger_read(slug)}: every task and its state, "
        "the operator's notes, answers, comments and chat.",
        f"Run once: {led} join --role orchestrator. {INBOX_LINE} Act on every OPERATOR line, then run {led} ack.",
        "",
        (
            "After posting your summary acknowledgement in ledger chat, send the ledger page link as your second chat line: "
            if summary
            else "Your first message after joining gives the operator the ledger page link: "
        )
        + f"run {me} url and post its line "
        f'with {me} say --to operator "<line>". Answer any question like what is my ledger link with that line at '
        "once.",
        "",
        "Your standing duties:",
        *([codex_master.waiting(slug)] if codex else []),
        *(
            [
                "When writing a handoff, make its Next action read agentihooks msg inbox and use "
                f"agentihooks swarm {slug} wait --inbox when no work remains."
            ]
            if codex
            else []
        ),
        "- Answer every operator chat message. Messages from the page reach this session as inbox messages from "
        'operator: answer each with agentihooks msg reply <id> "<text>", which shows the answer on the page and '
        f'closes the message. Post your own updates with {me} say --to operator "<text>". Answer what the operator '
        "types in this pane here.",
        "- A message that needs no work, such as a thanks, a confirmation or a reply that asks for nothing, carries "
        '--fyi: agentihooks msg send <address> --fyi "<text>", agentihooks msg reply <id> --fyi "<text>" or '
        f'{me} say "<text>" --to <agent name> --fyi. Its receiver closes it done with nothing more to name; a '
        "work item without --fyi closes with where the work went.",
        f'- Keep the ledger current: {led} phase <phase id> done|open, {led} comment phases/<phase id> "<status>", '
        f'{led} followup add "<text>", and {led} time-left "<duration>" whenever progress or blockers change it.',
        f"- {waiting_line(led)}",
        f"- {artifact_line(led)} When the operator asks for a file to review, add or set its task with --artifact "
        "or artifact=yes so its agent may publish it.",
        *peer_lines(task.get("peer", "")),
        *priming.master_lines(slug, task.get("peer", "")),
        f'- Turn each operator request into a task with a full spec: {led} task add <id> "<title>" --lane eng|ci '
        '--phase <phase id> --description "<seams and done condition>". Rewrite a task description with '
        f'{led} task set <id> description="<text>".',
        "- When the operator accepts a plan of yours, add its phases, then publish it before adding its tasks: "
        f"{led} publish-plan <plan file> --phase <phase ids>. {PUBLISHED}, links and comments each phase, and every "
        "task added to those phases carries the link.",
        *(
            [
                "- This swarm runs at full autonomy: turn a follow up an agent proposes into a task yourself, with "
                "the same task add, without asking the operator first."
            ]
            if autonomy == FULL
            else []
        ),
        plan_review.review_line(me, autonomy),
        *(
            [
                f'- Answer an agent\'s question you can decide with {led} answer questions/<id> "<answer>": it is '
                "recorded as yours and leaves the operator's Priorities. Raise the rest to the operator."
            ]
            if autonomy in (DELEGATE, FULL)
            else []
        ),
        f"- Steer the swarm when asked: {me} set max-eng-agents=N max-ci-agents=N max-plan-agents=N, {me} pause, {me} start, "
        f"{me} stop, {me} status.",
        f"- Give every new health finding a verdict once you have checked its evidence: {me} verdict <finding id> "
        f'{"|".join(VERDICTS)} --note "<why>". {me} status lists each finding with its id. A verdicted finding '
        "stays hidden for its cooldown, an hour by default, and comes back once only if its evidence grew.",
        f'- Talk to agents with {me} say "<text>" --to <agent name>, eng or ci, and relay operator words with '
        f'{me} send-message "<text>".',
        "- When an engineer merges work that changes a page, check it in a real browser on localhost "
        f"(the ledger page link {me} url prints) with the playwright-cmd tools, tell the operator what you "
        "saw, then close the shared browser with browser_close.",
        f'- Record a lesson the next master should know with {me} learned "<lesson because reason>".',
        f"- Raise a learned note that has held up: {me} learned lists every seat's notes with their numbers, and "
        f'{me} promote <seat> <number> insight|canon --reason "<why>" raises one. Only you and the operator make '
        "a note canon.",
        f"- Keep the swarm culture current: {me} culture show prints it, {me} culture set <file> replaces it. Every "
        "new occupant of every seat reads it.",
        "",
        "If your context nears its limit a hook tells you to write a handoff document: use the handoff skill "
        "to write what the operator asked for, what is pending and what you promised under the Handoff v2 headings, "
        f"run {me} handoff <doc> and stop. The next master continues from it; its recap is derived from the document.",
        "Write chat and comments in plain words for the operator: no ids, paths, hashes or dashes.",
    ]
    return "\n".join(lines) + "\n"


def peer_lines(peer):
    if not peer:
        return []
    return [
        f"- Your peer is the master of swarm {peer}, seat master@{peer}, on the linked ledger {peer}. Keep each "
        f'other in sync through the inbox: agentihooks msg send master@{peer} "<text>" for every change it needs '
        "to know, and answer each of its items with agentihooks msg reply."
    ]


def summary_lines(slug):
    from scripts.swarm_ledger.repository.folder import ledger_folder
    from scripts.swarm_ledger.repository.sqlite import read_ledger

    overview = str((read_ledger(ledger_folder(os.environ), slug, "overview") or {}).get("overview"))
    _, marker, summary = overview.partition(ledger_close.MARK)
    if not marker:
        return []
    return [
        "Ledger summary from the previous close:",
        f"Summary\n{summary}",
        "In your first ledger chat line, name this summary and say where the work stopped in plain words.",
    ]


def build_operator(slug, repo, name, role, profile):
    led = f"agentihooks ledger --slug {slug} --as {name}"
    leave = f"end this session with agentihooks swarm {slug} --as {name} exit."
    planner = role == "planner"
    lines = [
        f"You are {name}, a {profile} profile agent the operator launched on swarm {slug} over the repo {repo} with "
        f"agentihooks swarm {slug} {profile} up. You hold no task and no lane slot, and the swarm never nudges or "
        "retires you. You answer to the operator in this pane: wait for his first message.",
        f"Read the swarm ledger with {ledger_read(slug)} for context; change nothing on it "
        + ("until the operator accepts a plan." if planner else "unless the operator asks."),
        'Other sessions reach you as inbox messages: answer one with agentihooks msg reply <id> "<text>" and reach '
        f'the master with agentihooks msg send master@{slug} "<text>".',
        "",
    ]
    steps = operator_plan_steps(slug, led, leave) if planner else operator_work_steps(led, leave)
    return "\n".join([*lines, *steps])


def operator_work_steps(led, leave):
    return [
        "Work with the operator on what he asks in this pane. Make any code change in your own worktree (wt.sh new, "
        "the worktree skill) and a pull request into dev. Never join the ledger crew or claim a task. Propose other "
        f'work with {led} followup add "<plain words>".',
        f"When he says you are done, {leave}",
    ]


def operator_plan_steps(slug, led, leave):
    return [
        "Plan with the operator here. Ask what you need, edit no code, and revise until he accepts the plan. On his "
        "accept, in this order:",
        "1. Write the plan as markdown, giving each phase its own heading whose text is exactly that phase's title, "
        "and its phases as JSON, the init-swarm content phases shape with planning manual on each phase, in a folder "
        "from agentihooks scratch new.",
        f"2. Join the ledger crew: {led} join.",
        f"3. Append the phases: {led} plan phases <phases file>. Note the phase ids it prints.",
        f"4. Publish the plan: {led} publish-plan <plan file> --phase <phase ids>. {PUBLISHED}, and links and "
        "comments each phase.",
        f"5. Add each phase's tasks: {led} task add - <title> --phase <id> --lane <eng or ci> --kind <kind> "
        '--description "<scope and Done when sentence>" --depends-on <ids> --territory <areas>. The ledger takes '
        "tasks only in phases you appended, and each carries the plan link.",
        f'6. Tell the master: agentihooks msg send master@{slug} "<the plan link and the phase and task ids you added; '
        'the phases wait on plan review before engineers claim them>".',
        f"7. Leave the crew with {led} leave, tell the operator here what you registered, then {leave}",
    ]


def build_dispatcher(slug: str, repo: str, name: str, task: dict, autonomy: str = DELEGATE) -> str:
    from scripts.swarm import dispatch_seat

    me = f"agentihooks swarm {slug}"
    led = f"agentihooks ledger --slug {slug} --as {name}"
    lines = [
        f"You are {name}, the dispatcher of swarm {slug}, working beside its master in the repo {repo}. "
        f"The swarm runs at {autonomy} autonomy.",
        "The tick woke you because its deterministic passes could not settle these triggers:",
        *(dispatch_seat.line(trigger) for trigger in task.get("triggers", [])),
        *priming_lines(task),
        "",
        f"Before anything else, run once: {led} join. Then read the ledger with {ledger_read(slug)} and each trigger's "
        "item in it.",
        "Settle each trigger within the swarm's autonomy with the agentihooks commands, the classifiers and read only sub "
        f'agents: comment on its item with {led} comment <item> "<text>", close a decided follow up, rank a task, and '
        f"clear the priority once it is resolved with {led} priority clear <priority id>.",
        "A decision only the operator can make goes to the master; never ask the operator yourself. You never edit "
        "code or config files, commit, merge or claim a task.",
        f'After each trigger, tell the master what you did: agentihooks msg send {MASTER}@{slug} "<plain words>".',
        'New triggers arrive as inbox messages: answer one with agentihooks msg reply <id> "<text>", or close it with '
        'agentihooks msg close <id> done "<where the work went>".',
        f'While you wait on a trigger, declare it: {me} wait 30 --reason "<what you wait on>".',
        f"When every trigger is closed, run {led} leave, then {me} done and stop: the swarm ends your session.",
        "Write ledger comments and messages in plain words: no ids, paths, hashes or dashes.",
    ]
    return "\n".join(lines) + "\n"


def build(slug, repo, lane, name, task, role="", autonomy=DELEGATE):
    if lane == MASTER:
        return build_master(slug, repo, name, task, autonomy)
    if lane == DISPATCH:
        return build_dispatcher(slug, repo, name, task, autonomy)
    me = f"agentihooks swarm {slug}"
    led = f"agentihooks ledger --slug {slug} --as {name}"
    phase = task.get("phase") or "<phase id>"
    lines = [
        f"You are {name}, {role or LANE_ROLE[lane]} in swarm {slug}, working in the repo {repo}.",
        f"Your one task for this session is {task['id']}: {task['title']}",
    ]
    if task.get("description"):
        lines.append(task["description"])
    if task.get("plan_lines"):
        lines.append(plan_read.pointer(task))
    lines += contract_lines(task.get("contract") or {})
    lines += workspace_lines(task)
    lines += group_lines(task)
    lines += overlap_lines(task)
    if task.get("pr_url"):
        lines.append(f"An earlier agent already opened {task['pr_url']}: continue it instead of starting over.")
    lines += continuation_lines(task)
    lines += priming_lines(task)
    if lane == "ci":
        lines.append(
            f'Propose a further bottleneck as a follow up: {led} followup add "<plain words naming it>". '
            "Never queue it as a task yourself: the master or the operator decides."
        )
    lines += [
        "",
        f"You are a member of the ledger crew. Before anything else, run once: {led} join. {INBOX_LINE} "
        f"Act on every OPERATOR line about your work, then run {led} ack.",
        "",
        f"Then read the ledger in full with {ledger_read(slug)}: every task and its state, "
        "the operator's notes, answers and comments. It is your starting point; take only your own task.",
        "Page chat is for the master: act on a chat line only when it starts with @ and your name.",
        f'Keep the ledger current as you go: {led} comment phases/{phase} "<what you did>" when your work lands, '
        f'{led} followup add "<text>" for a blocker or follow up you find. A hook blocks your stop while operator '
        "events are unhandled or you have gone many tool calls without a ledger command.",
        waiting_line(led),
        artifact_line(led),
        f'Record a lesson the next occupant of your seat should know with {me} learned "<lesson because reason>" (a note; add '
        "--maturity data for a raw figure or insight for one that held up more than once).",
        "",
        *task_steps(task, me, led, name, phase, autonomy),
        "",
        "If your context nears its limit a hook tells you to write a handoff document: use the handoff skill "
        f"for the Handoff v2 body with what you did, where you stopped and what you promised, then run {me} handoff <doc> and stop; a "
        "successor continues the task from it and the seat recap is derived from the same document.",
        (
            f'If you cannot finish: {me} block "<plain words naming the blocker>" and stop.'
            if ledger_kinds.kind(task) == "plan"
            else "If you cannot finish (missing secret, a decision only the operator can make, another task first): push your "
            f'branch, open a draft pull request, then {me} block "<plain words naming the blocker>" and stop.'
        ),
        "",
        f'Talk to the swarm and the operator with {me} say "<text>" (add --to <agent name>, eng or ci). '
        'Messages for you arrive in this session as inbox messages: answer one with agentihooks msg reply <id> "<text>". '
        "Add --fyi after the id or address of a thanks or confirmation that needs no work. "
        "Write chat and comments in plain words for the operator: no ids, "
        "paths, hashes or dashes.",
        "Other agents work other tasks in parallel. Touch only what your task needs.",
    ]
    return "\n".join(lines) + "\n"


def waiting_line(led):
    return (
        "Whenever you wait on the operator, raise it where he looks instead of only writing it in chat: "
        f'{led} priority add <item> "<the ask in plain words>", where the item is phases/<id>, questions/<id>, '
        f'followups/<id> or tasks/<id>, or {led} followup add "<text>" --needs-operator for a decision only he can '
        "make. Unanswered questions, blocked tasks and merge approvals show in Priorities on their own."
    )


def artifact_line(led):
    return (
        "Publish an artifact only when the operator asked for that file: a plan, an image, a logo, an SVG, markdown "
        f'or JSON he wants to review. Publish it with {led} artifact <file> "<title in plain words>" from a task '
        "marked artifact requested, or add --request <id of his message that asked>. Never publish test runs, logs, "
        "review notes or proofs: proofs go on the task proof and the pull request, raw output stays in the task "
        "work folder."
    )


def priming_lines(task):
    seat = f"Your seat {task['seat']}" if task.get("seat") else "Your seat"
    handoff, recaps, learned = task.get("handoff", ""), task.get("recaps") or [], task.get("learned") or []
    culture = task.get("culture", "")
    if not (handoff or culture or recaps or learned):
        return [f"{seat} has no history yet: no handoff document, no recap and no learned notes."]
    lines = [f"{seat} carries what earlier occupants left. Read it in this order:"]
    if task.get("transfer"):
        from scripts.handoff import transfers

        lines.append(transfers.priming(task["seat"].rsplit("@", 1)[1], task["transfer"]))
    if handoff:
        lines += handoff_lines(task)
    else:
        lines.append("1. Handoff document: none was left for this task.")
    if culture:
        lines += ["2. Swarm culture, shared by every seat of this swarm:", culture]
    else:
        lines.append("2. Swarm culture: none written for this swarm yet.")
    if recaps:
        lines += [f"3. Latest recap, {_by(recaps[0])}:", recaps[0]["text"]]
    else:
        lines.append("3. Latest recap: missing, no occupant of this seat left one.")
    lines += learned_lines(learned)
    older = recaps[1:]
    if not older:
        return [*lines, "5. Older recaps: none."]
    lines.append("5. Older recaps, newest first:")
    for recap in older[:OLDER_RECAPS]:
        lines += [f"{_by(recap)}:".capitalize(), recap["text"]]
    if len(older) > OLDER_RECAPS:
        lines.append(f"{len(older) - OLDER_RECAPS} older recaps are kept on the seat and not shown.")
    return lines


def handoff_lines(task):
    envelope = task.get("handoff_envelope")
    lines = [
        "1. Handoff runtime envelope:",
        json.dumps(envelope, indent=2) if envelope else "Missing runtime envelope.",
    ]
    lines += ["Handoff document: continue from it:", task["handoff"]]
    reading = section(task["handoff"], "Read first")
    if reading:
        lines += [
            "Read first: Open each address in rank order and answer its question before reading older recaps.",
            reading,
        ]
    else:
        lines.append("Read first: missing from the handoff document.")
    return lines


def _verdict(task):
    return task.get("handoff_envelope") or task.get("reclaim") or {}


def _continue_from(task):
    ref = _verdict(task).get("continue_from")
    return ref if ref and ref.startswith("origin/") else ""


def continuation_lines(task):
    envelope = _verdict(task)
    ref = _continue_from(task)
    if task.get("parked_on"):
        return []
    if ref:
        branch = ref.removeprefix("origin/")
        return [
            f"Your predecessor's branch {branch} is on the remote at {envelope['remote_head']}: continue it. The "
            f"worktree step below cuts your worktree from it; push with git push -u origin HEAD:{branch} so its "
            "commits and its pull request carry on."
        ]
    if envelope.get("continue_from") == "fresh":
        return [f"Start your worktree fresh from dev: {envelope['fresh_reason']}."]
    return []


def continued(steps, name, task):
    ref = _continue_from(task)
    if not ref:
        return steps
    cut = f"wt.sh new {naming.plain(name)}"
    return [step.replace(cut, f"{cut} --from {ref}") for step in steps]


def learned_lines(learned):
    rank = {maturity: -level for level, maturity in enumerate(MATURITIES)}
    learned = [note for note in learned if not note.get("withheld") and "retired" not in note]
    shown = sorted((note for note in learned if note["maturity"] != "data"), key=lambda note: rank[note["maturity"]])
    data = len(learned) - len(shown)
    counted = [f"{data} data entries are kept on the seat and not shown."] if data else []
    if shown:
        return [
            "4. Learned notes, canon first:",
            *(f"- {note['maturity']}: {note['text']}" for note in shown),
            *counted,
        ]
    if data:
        return ["4. Learned notes: only data so far.", *counted]
    return ["4. Learned notes: none recorded on this seat yet."]


def _by(recap):
    return f"by {recap['occupant']} on task {recap['task']}"


def contract_lines(contract):
    parts = [f"{label}: {contract[key]}." for key, label in CONTRACT_LABELS if contract.get(key)]
    return ["Proof contract. " + " ".join(parts)] if parts else []


def workspace_lines(task):
    if not task.get("workspace"):
        return []
    return [
        f"Your work folder is {task['workspace']}. Read steering.md there first. Append a line to progress.md each "
        "time a step lands and to proof.md for each piece of evidence (a test run, a check link, a command and its "
        "output), so a successor can continue from the folder alone."
    ]


def overlap_lines(task):
    overlaps = task.get("overlaps") or []
    if not overlaps:
        return []
    return [OVERLAP_LINE] + [
        f"Task {o['task']}, claimed by {o['claimant']}, shares {' and '.join(o['areas'])}." for o in overlaps
    ]


def group_lines(task):
    group = task.get("group") or []
    if not group:
        return []
    return [GROUP_LINE] + [f"Task {m['id']}: {m['title']}. {m['description']}".rstrip() for m in group]


def issue_step(me, what):
    return (
        "1. If the repo has GitHub issues (gh repo view --json hasIssuesEnabled), open an issue naming "
        f"{what} and record it: {me} issue <issue url>. Without issues the ledger task is the spec: skip this step."
    )


def code_steps(me, led, name, phase):
    return [
        "Work it end to end with the dev-cycle skill, then stop:",
        issue_step(me, "the seams"),
        f"2. Create your worktree: wt.sh new {name} (never edit the primary checkout), then call {SERENA}",
        "3. Red test, least code to green.",
        "4. Gates green (ruff check, ruff format --check and the tests), commit in the worktree; at your first commit "
        f"push the branch (git push -u origin HEAD) and record it: {me} branch. Then review per the "
        "dev-cycle skill: at most two critic sub agents, Standards and Spec, that never edit and send every finding "
        "back to you; fix each finding, the same reader re-reviews, and review closes after three rounds.",
        f"5. Push, open the pull request into dev (with Closes #<n> when there is an issue), record it: {me} pr <pr url>",
        f"6. Wait on the checks with {me} wait --on checks <pr url>: the tick ends the wait and tells you when they "
        f"resolve, so no Monitor is needed. Queue on green checks with {me} merge queue <pr url>, then "
        f"{me} wait --on merge <pr url>. Report queue state with {me} merge state <pr url>. "
        f"Before fixing a queued pull request, dequeue first with {me} merge dequeue <pr url>, then push, "
        f"then queue again with {me} merge queue <pr url> once checks pass. "
        "Keep the worktree until the tick confirms merged; a red merge wait means fix the pull request and "
        "queue it again. After merged, run wt.sh done.",
        f"7. Leave the crew with {led} leave, then close the task: {me} done --pr <pr url>. {CLOSES}",
    ]


def reuse_issue_step(me, task):
    if task.get("issue_url"):
        return f"1. Reuse the task's issue {task['issue_url']}; open no new one."
    return issue_step(me, "the seams")


def stacked_steps(me, led, name, task):
    first, *others = task["stack_base"]
    ref = _continue_from(task) or f"origin/{first['branch']}"
    merges = "".join(f" Merge each other open dependency into it: git merge origin/{dep['branch']}." for dep in others)
    return [
        "Your task depends on work still open. Build on top of it, push and park; the next engineer finishes it "
        "once its dependency is done:",
        *(f"Dependency {dep['task']} is still open on branch {dep['branch']}." for dep in task["stack_base"]),
        reuse_issue_step(me, task),
        f"2. Create your worktree from the dependency branch: wt.sh new {name} --from {ref}, then call {SERENA}"
        f"{merges}",
        "3. Build what you can on top of the dependency: red test, least code to green. Leave what needs the "
        "dependency finished and name it in your handoff.",
        "4. Gates green on what you built, commit with the issue in the message (Refs #<n>) so the branch points at "
        f"the issue, push the branch (git push -u origin HEAD) and record it: {me} branch.",
        "5. Write a Handoff v2 document with the handoff skill whose Stopped at names the open dependency and what "
        f"waits on it. Leave the crew with {led} leave, then park: {me} park <doc>. Park comments the branch on the "
        f"issue so they point at each other, and the task waits on its branch. {CLOSES}",
    ]


def finish_steps(steps, me, name, task):
    branch = task["branch"]
    cut = f"wt.sh new {name}"
    steps = [step.replace(cut, f"{cut} --from origin/{branch}") for step in steps]
    steps[1] = reuse_issue_step(me, task)
    steps[2] += RESTACK.format(me=me)
    return [f"Your task was parked on branch {branch} until its dependency finished.", *steps]


def task_steps(task, me, led, name, phase, autonomy):
    kind = ledger_kinds.kind(task)
    steps = kind_steps(kind, me, led, name, phase, autonomy, task["id"])
    if kind not in STACKED_KINDS:
        return continued(steps, name, task)
    if task.get("parked_on"):
        return finish_steps(steps, me, naming.plain(name), task)
    if task.get("stack_base"):
        return stacked_steps(me, led, naming.plain(name), task)
    return continued(steps, name, task)


def ci_steps(me, led, name, phase):
    steps = code_steps(me, led, name, phase)
    steps[3] = (
        "3. Red first on a real run: show the workflow failing or slow, then the least change that turns it green "
        "or fast. Put the run links before and after in the pull request."
    )
    return steps


def ops_steps(me, led, name, phase):
    return [
        "Work it end to end, then stop:",
        issue_step(me, "the system state this task must reach"),
        f"2. {THROUGH_CODE.format(name=name)}",
        "3. Verify the state against the live system with one read only command and keep its output.",
        f'4. Leave the crew with {led} leave, then close the task with the proof: {me} done --command "<command>" '
        f'--output "<its output>". The ledger refuses done without both. {CLOSES}',
    ]


def tune_steps(me, led, name, phase):
    return [
        "Work it end to end, then stop:",
        issue_step(me, "the setting and the number it must reach"),
        "2. Measure the current value with one read only command before changing anything.",
        f"3. {THROUGH_CODE.format(name=name)}",
        "4. Once the change runs, measure again with the same command.",
        f'5. Leave the crew with {led} leave, then close the task with the proof: {me} done --command "<command>" '
        f'--output "<its output>", with the values before and after in the output. The ledger refuses done without '
        f"both. {CLOSES}",
    ]


def troubleshoot_steps(me, led, name, phase):
    return [
        "Work it end to end, then stop:",
        issue_step(me, "the symptom"),
        "2. Reproduce the failure before any theory, then test ranked hypotheses one at a time with the "
        "quick-troubleshoot skill. Diagnostics stay read only.",
        "3. Show the root cause by evidence: a command and its output, a log line or a failing test.",
        f"4. Fix it with a pull request into dev through the dev-cycle skill in a worktree (wt.sh new {name}), or "
        f'propose the fix as a follow up for the master: {led} followup add "<plain words>"',
        f'5. Leave the crew with {led} leave, then close the task with the proof: {me} done --root-cause "<cause>" '
        '--evidence "<what shows it>" --fix <pr url>, or --filed "<the follow up you proposed>" instead of --fix. '
        f"The ledger refuses done without the cause, the evidence and the fix or the filed follow up. {CLOSES}",
    ]


def research_steps(me, led, name, phase):
    return [
        "Work it end to end, then stop:",
        issue_step(me, "the question"),
        "2. Answer it from sources you read yourself: code, docs, runs and their output. Name each source.",
        "3. Write the finding where others can read it later: a comment on the issue or the ledger task, or a document merged into dev "
        "by pull request.",
        f"4. Leave the crew with {led} leave, then close the task with the proof: {me} done --finding <link>. The "
        f"ledger refuses done without a link to the finding. {CLOSES}",
    ]


def plan_steps(me: str, led: str, name: str, phase: str) -> list[str]:
    return [
        "Work it end to end, then stop:",
        "1. Read steering.md: project and mission intent, dependency phase evidence and any review note.",
        f"2. Slice only phase {phase}. Edit no code. Write the slice as a markdown plan: the plan you leave plan mode "
        "with, or plan.md in your work folder.",
        f"3. Publish the plan before adding tasks: {led} publish-plan <plan file> --phase {phase}. {PUBLISHED}, links "
        "and comments the phase, and every task you add in this phase carries the link.",
        f"4. Add tasks with {led} task add <id> <title> --phase {phase} "
        '--lane <eng or ci> --kind <kind> --description "<scope and Done when sentence>" '
        "--depends-on <ids> --territory <areas>; include --must, --check and --judge for work beyond code.",
        "5. Keep each code or ci task to one pull request, at most six territory areas and twelve tasks in the slice.",
        f'6. Propose work outside this phase as a follow up: {led} followup add "<plain words>".',
        f"7. Leave the crew with {led} leave, then close with {me} done --slice <ids>, the comma separated ids "
        f"of the tasks you added in this phase. The ledger refuses invalid slice ids and slice tasks without the "
        f"plan link. {CLOSES}",
    ]


STEPS = {
    "plan": plan_steps,
    "code": code_steps,
    "ci": ci_steps,
    "ops": ops_steps,
    "tune": tune_steps,
    "troubleshoot": troubleshoot_steps,
    "research": research_steps,
}


def manual_ship(me, led, task_id):
    return [
        "5. Push, open a draft pull request into dev (gh pr create --draft --base dev, with Closes #<n> when there "
        f"is an issue), record it: {me} pr <pr url>",
        "6. This swarm runs at manual autonomy: you never merge. The operator reviews the draft and merges it.",
        f'7. Leave the crew with {led} leave, then hand the draft to the operator: {me} block "<plain words: the '
        f'draft pull request is ready for your review and merge>". {CLOSES}',
    ]


def assist_ship(me, led, task_id):
    return [
        f"5. Push, open the pull request into dev (with Closes #<n> when there is an issue), record it: {me} pr <pr url>",
        "6. This swarm runs at assist autonomy. Once checks are green, ask the operator to approve the merge: "
        f'{led} comment tasks/{task_id} "<plain words: what the pull request does, checks green, waiting for your '
        f'approval to merge>", then {me} wait 60 --reason "operator merge approval"; his answer reaches you as an '
        f"inbox message. Queue with {me} merge queue <pr url> only after an OPERATOR line on the ledger approves "
        f"it, then {me} wait --on merge <pr url>. Report queue state with {me} merge state <pr url>. "
        f"Before fixing a queued pull request, dequeue first with {me} merge dequeue <pr url>, then push, "
        f"then queue again with {me} merge queue <pr url> once checks pass and approval still holds. "
        "Keep the worktree until the tick confirms merged; a red merge wait means fix the pull request "
        "and queue it again. After merged, run wt.sh done. An OPERATOR "
        "line asking for changes: make them and ask again.",
        f"7. Leave the crew with {led} leave, then close the task: {me} done --pr <pr url>. {CLOSES}",
    ]


GATED_SHIP = {MANUAL: manual_ship, ASSIST: assist_ship}


def kind_steps(kind, me, led, name, phase, autonomy, task_id):
    steps = STEPS[kind](me, led, naming.plain(name), phase)
    if kind not in ("code", "ci") or autonomy not in GATED_SHIP:
        return steps
    return steps[:5] + GATED_SHIP[autonomy](me, led, task_id)
