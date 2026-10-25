"""Swarm tasks: claimable units of work under a phase, each in the eng or ci lane."""

import re
from pathlib import Path

import ledger_comments
import ledger_kinds

from scripts.swarm_ledger import ledger_plans, ledger_rank, ports, task_dependencies

AUTHOR_RE = re.compile(r"^[A-Za-z][\w.@-]{0,63}$")
ID_RE = re.compile(r"^[A-Za-z0-9][\w.-]{0,63}$")
ITEM_RE = re.compile(r"^tasks/[^/]+$")
MINTED_RE = re.compile(r"t(\d+)")
LANES = ("eng", "ci", "plan")
STATES = ("open", "claimed", "blocked", "pr", "done")
UPDATABLE = (
    "state",
    "claimed_by",
    "issue_url",
    "pr_url",
    "description",
    "depends_on",
    "territory",
    "kind",
    "contract",
    "proof",
    "workspace",
    "awaiting",
    "artifact",
    "profile",
    "plan_url",
    "plan_slice",
    "rank",
    "branch",
    "branch_repo",
    "repositories",
    "repository_branches",
    "stacked_base",
    "parked_on",
    "parked_repos",
    "overlays",
    "difficulty",
    "difficulty_source",
    "difficulty_confidence",
    "phase",
    "slice",
    "follow_up",
    "split_pulls",
    "pull_scopes",
    "after_release",
    "applied_at",
    "workstation",
    "doc_items",
    "watchdog",
    "recovery",
)
BOOL_FIELDS = ("artifact", "follow_up", "workstation")
DIFFICULTIES = ("S", "M", "L")
DIFFICULTY_SOURCES = ("operator", "rule", "classifier", "default")
DIFFICULTY_FIELDS = ("difficulty", "difficulty_source", "difficulty_confidence")
NUMBER_FIELDS = ("difficulty_confidence",)
LIST_FIELDS = (
    "depends_on",
    "territory",
    "parked_on",
    "parked_repos",
    "overlays",
    "split_pulls",
    "repositories",
    "doc_items",
)
OVERLAY_CAP = 3
BRANCH_RE = re.compile(r"^(?!-)\S*$")
COMMIT_RE = re.compile(r"^([0-9a-f]{7,64})?$")
RELEASE_RE = re.compile(r"^(v?\d+\.\d+\.\d+)?$")
APPLIED_RE = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)?")
OBJECT_FIELDS = ("contract", "proof", "recovery", "repository_branches", "pull_scopes", "watchdog")
PULL_SCOPE_KINDS = ("stage", "prerequisite")
PULL_URL_RE = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+")
URL_FIELDS = ("issue_url", "pr_url", "plan_url")
URL_RE = re.compile(r"^https?://[^\s]+$")
OPS = ("task_add", "task_update")
PARENT_FIELDS = {"slice", "phase", "plan_slice", "plan_lines"}
WORKER_LANES = ("eng", "ci")
SWARM = "swarm"
RANK_AUTHORS = ("operator", SWARM, "dispatcher")
RANK_LANES = ("master", "dispatch")
QUOTED = "task_update takes quote, the operator's words, only with a rank"
UNRELAYED_TASK = "a rank relay refused: the task does not exist"
UNRELAYED_ROLE = (
    "a rank relay refused: {by} is not the ledger's orchestrator; only the master relays the operator's rank, "
    "anyone else sets the rank without a quote"
)
UNRELAYED_WORDS = (
    "a rank relay refused: the quote is not at least three of the operator's exact words the hooks recorded "
    "in a master or planner session of this swarm: quote his exact words or set the rank without a quote"
)
UNRELAYED_RANK = (
    "a rank relay refused: the operator's quoted words do not name the rank {rank}: quote words that say it "
    "or set the rank without a quote"
)
PROPOSE = 'propose the work with agentihooks ledger followup add "<plain words>" and the master decides'
PUBLISH = (
    "publish the plan with agentihooks ledger publish-plan <file> --phase <phase id>, then link each task with "
    "task set <id> plan_url=<link>"
)


def check_lane(task: dict) -> None:
    if (ledger_kinds.kind(task) == "plan") != (task.get("lane", "eng") == "plan"):
        raise ValueError("plan tasks must use the plan lane, and the plan lane accepts only plan tasks")


def check(op):
    by = op.get("by")
    if not isinstance(by, str) or not AUTHOR_RE.match(by) or by == "operator":
        raise ValueError("task ops need `by`, an agent name other than operator")
    if op["op"] == "task_add":
        if (
            not isinstance(op.get("task"), str)
            or not ID_RE.match(op["task"])
            or not isinstance(op.get("title"), str)
            or not op["title"].strip()
        ):
            raise ValueError("task_add needs task <id> and title")
        if op.get("lane") not in LANES:
            raise ValueError(f"lane must be one of {LANES}")
        if not all(isinstance(op.get(key, ""), str) for key in ("phase", "description", "workspace", "slice")):
            raise ValueError("phase, description, workspace and slice must be strings")
        if "not_duplicate" in op and not (isinstance(op["not_duplicate"], str) and op["not_duplicate"].strip()):
            raise ValueError("not_duplicate must say in plain words why the task differs from the one it resembles")
        check_lists(op)
        check_bools(op)
        check_profile(op)
        check_urls(op)
        check_rank(op)
        check_difficulty(op)
        ledger_kinds.check(op)
        check_lane(op)
        gain = op.get("gain", 0)
        if isinstance(gain, bool) or not isinstance(gain, (int, float)) or gain < 0:
            raise ValueError("gain must be a nonnegative number")
        ledger_comments.check(op["title"], "item")
        return
    fields = op.get("fields")
    if not ITEM_RE.match(str(op.get("item"))) or not isinstance(fields, dict) or not fields:
        raise ValueError("task_update needs item tasks/<id> and fields")
    check_difficulty(fields)
    strings = {k: v for k, v in fields.items() if k not in LIST_FIELDS + OBJECT_FIELDS + BOOL_FIELDS + NUMBER_FIELDS}
    if set(fields) - set(UPDATABLE) or not all(isinstance(v, str) for v in strings.values()):
        raise ValueError(
            f"task_update may set only {UPDATABLE}, as strings, {LIST_FIELDS} as lists or {OBJECT_FIELDS} as objects"
        )
    if "state" in fields and fields["state"] not in STATES:
        raise ValueError(f"state must be one of {STATES}")
    check_guards(op)
    check_quote(op)
    check_lists(fields)
    check_stack(fields)
    check_release(fields)
    check_applied(fields)
    check_bools(fields)
    check_profile(fields)
    check_urls(fields)
    check_rank(fields)
    ledger_kinds.check(fields)


def check_guards(op: dict) -> None:
    if not isinstance(op.get("if_plan_lines_missing", False), bool):
        raise ValueError("if_plan_lines_missing must be a boolean")
    guard = op.get("if_state", [])
    if not isinstance(guard, list) or not all(state in STATES for state in guard):
        raise ValueError(f"if_state must be a list of states from {STATES}")
    expected = op.get("if_match", {})
    if not isinstance(expected, dict) or set(expected) - {"claimed_by", "claim_generation", "pr_url", "awaiting"}:
        raise ValueError("if_match must contain task ownership fields")
    if any(not isinstance(value, str) for key, value in expected.items() if key != "claim_generation"):
        raise ValueError("if_match ownership fields must be strings")
    generation = expected.get("claim_generation", 0)
    if type(generation) is not int or generation < 0:
        raise ValueError("if_match claim_generation must be a nonnegative integer")


def _guarded(task, op):
    expected = op.get("if_match", {})
    return (bool(op.get("if_state")) and task.get("state", "open") not in op["if_state"]) or any(
        task.get(key, 0 if key == "claim_generation" else "") != value for key, value in expected.items()
    )


def check_quote(op: dict) -> None:
    if "quote" not in op:
        return
    quote = op["quote"]
    if not isinstance(quote, str) or not quote.strip() or set(op["fields"]) != {"rank"}:
        raise ValueError(QUOTED)


def check_stack(fields):
    if "branch" in fields and not (isinstance(fields["branch"], str) and BRANCH_RE.match(fields["branch"])):
        raise ValueError("branch must be a git branch name, or empty to clear it")
    if "branch_repo" in fields and not BRANCH_RE.match(fields["branch_repo"]):
        raise ValueError("branch_repo must be a repository url or path, or empty to clear it")
    if not all(BRANCH_RE.match(repo) for repo in fields.get("parked_repos", [])):
        raise ValueError("parked_repos must list repository urls or paths")
    base = fields.get("stacked_base", "")
    if not (isinstance(base, str) and COMMIT_RE.match(base)):
        raise ValueError("stacked_base must be a lowercase commit hash of 7 to 64 characters, or empty to clear it")
    if not all(ID_RE.match(task_id) for task_id in fields.get("parked_on", [])):
        raise ValueError("parked_on must list task ids")


def check_release(fields):
    value = fields.get("after_release", "")
    if not (isinstance(value, str) and RELEASE_RE.fullmatch(value)):
        raise ValueError("after_release must be a release tag such as v2.17.1, or empty to clear it")


def check_applied(fields):
    value = fields.get("applied_at", "")
    if not (isinstance(value, str) and APPLIED_RE.fullmatch(value)):
        raise ValueError("applied_at must be a UTC time such as 2026-10-18T12:00:00Z, or empty to clear it")


def check_rank(fields):
    if "rank" in fields:
        ledger_rank.canonical(fields["rank"])


def check_difficulty(fields):
    if not set(DIFFICULTY_FIELDS) & set(fields):
        return
    if "difficulty" not in fields:
        raise ValueError("difficulty_source and difficulty_confidence come with a difficulty")
    fields = sized(fields)
    if fields["difficulty"] not in DIFFICULTIES:
        raise ValueError(f"difficulty must be one of {DIFFICULTIES}")
    if fields["difficulty_source"] not in DIFFICULTY_SOURCES:
        raise ValueError(f"difficulty_source must be one of {DIFFICULTY_SOURCES}")
    confidence = fields["difficulty_confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("difficulty_confidence must be a number from 0 to 1")


def sized(fields):
    if "difficulty" not in fields:
        return fields
    return {"difficulty_source": "operator", "difficulty_confidence": 1.0, **fields}


def check_repository_branches(fields: dict) -> None:
    branches = fields.get("repository_branches", {})
    if not isinstance(branches, dict) or not all(
        isinstance(repo, str)
        and Path(repo).is_absolute()
        and isinstance(branch, str)
        and branch
        and BRANCH_RE.match(branch)
        for repo, branch in branches.items()
    ):
        raise ValueError("repository_branches must map absolute local repository paths to branch names")


def check_pull_scopes(fields: dict) -> None:
    scopes = fields.get("pull_scopes", {})
    if not isinstance(scopes, dict) or not all(
        PULL_URL_RE.fullmatch(url)
        and isinstance(declared, dict)
        and set(declared) == {"kind", "scope"}
        and declared["kind"] in PULL_SCOPE_KINDS
        and isinstance(declared["scope"], str)
        and declared["scope"].strip()
        for url, declared in scopes.items()
    ):
        raise ValueError(
            "pull_scopes must map pull request URLs to a kind, stage or prerequisite, and a nonempty scope"
        )


def check_lists(fields):
    check_repository_branches(fields)
    check_pull_scopes(fields)
    for key in LIST_FIELDS:
        value = fields.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise ValueError(f"{key} must be a list of nonempty strings")
    if not all(Path(repo).is_absolute() for repo in fields.get("repositories", [])):
        raise ValueError("repositories must list absolute local repository paths")
    if len(set(fields.get("overlays", []))) > OVERLAY_CAP:
        raise ValueError(f"overlays lists at most {OVERLAY_CAP} overlay names")


def check_bools(fields):
    for key in BOOL_FIELDS:
        if key in fields and not isinstance(fields[key], bool):
            raise ValueError(f"{key} must be true or false")


def check_profile(fields):
    if "profile" in fields and not (
        fields["profile"] == "" or isinstance(fields["profile"], str) and ID_RE.match(fields["profile"])
    ):
        raise ValueError("profile must be a profile name such as frontend, or empty for the lane profile")


def check_urls(fields):
    for key in URL_FIELDS:
        if fields.get(key) and not URL_RE.match(fields[key]):
            raise ValueError(f"{key} must be an http or https link")


def check_task(task):
    """Rules a task obeys however it arrives: op, seed edit or new ledger."""
    if task.get("lane", "eng") not in LANES:
        raise ValueError(f"tasks/{task.get('id')}/lane must be one of {LANES}")
    if task.get("state", "open") not in STATES:
        raise ValueError(f"tasks/{task.get('id')}/state must be one of {STATES}")
    check_lists(task)
    check_stack(task)
    check_release(task)
    check_applied(task)
    check_urls(task)
    check_profile(task)
    check_rank(task)
    check_difficulty(task)
    ledger_kinds.check(task)
    check_lane(task)
    if task.get("state") == "done" and ledger_kinds.unmet(task):
        raise ValueError(f"tasks/{task.get('id')} is done without its proof: {', '.join(ledger_kinds.unmet(task))}")


def _add(doc, op, ctx):
    op = ledger_plans.with_slice(doc, ledger_plans.with_plan_slice(doc, op))
    tasks = doc.setdefault("tasks", [])
    if taken := next((t for t in tasks if t["id"] == op["task"]), None):
        ctx.refused.append(
            f'task {taken["id"]} already exists as "{taken["title"]}" ({taken.get("state", "open")}): '
            "pick another id, or pass - as the id to mint one"
        )
        return False
    if not _known(tasks, op.get("depends_on", [])):
        missing = sorted(set(op["depends_on"]) - {t["id"] for t in tasks})
        ctx.refused.append(f"task {op['task']} depends on unknown tasks: {', '.join(missing)}")
        return False
    appended = {p["id"] for p in doc.get("phases", []) if p.get("added_by") == op["by"]}
    if refusal := (
        add_refusal(tasks, op, appended)
        or ("rank" in op and rank_refusal(op["by"]))
        or follow_up_refusal(op)
        or unsliced_refusal(doc, op, op["by"])
    ):
        ctx.refused.append(refusal)
        return False
    task = {
        "id": op["task"],
        "title": op["title"].strip(),
        "description": op.get("description", ""),
        "phase": op.get("phase", ""),
        "lane": op["lane"],
        "state": "open",
        "claimed_by": "",
        "issue_url": "",
        "pr_url": "",
        "depends_on": op.get("depends_on", []),
        "territory": op.get("territory", []),
        "kind": op.get("kind", "code"),
        "done": False,
        "comments": [],
    }
    for key in (
        "gain",
        "contract",
        "workspace",
        "artifact",
        "profile",
        "overlays",
        "not_duplicate",
        "repositories",
        "repository_branches",
        "slice",
        "follow_up",
        "workstation",
        *ledger_kinds.WORKFLOW_FIELDS,
    ):
        if key in op:
            task[key] = op[key]
    if "rank" in op:
        task["rank"] = ledger_rank.canonical(op["rank"])
    task.update({k: v for k, v in sized(op).items() if k in DIFFICULTY_FIELDS})
    phase = next((p for p in doc.get("phases", []) if p["id"] == task["phase"]), {})
    if plan_url := op.get("plan_url") or phase.get("plan_url"):
        task["plan_url"] = plan_url
    if "plan_slice" in op:
        from scripts.swarm_ledger import plan_ranges

        try:
            task["plan_lines"] = plan_ranges.task_slice(doc, phase, op["plan_slice"], task.get("plan_url", ""))
        except ValueError as exc:
            ctx.refused.append(str(exc))
            return False
        task["plan_slice"] = op["plan_slice"]
    if refusal := (
        ledger_plans.task_refusal(doc, task) or shared_refusal(doc, task) or ledger_rank.urgent_refusal(task, doc)
    ):
        ctx.refused.append(refusal)
        return False
    tasks.append(task)
    ctx.record(op["by"], "added", f"tasks/{task['id']}", text=task["title"])
    if "rank" in task:
        ctx.stamp(f"tasks/{task['id']}/rank", op["by"])
    return True


def add_refusal(tasks, op, appended):
    lane_of = ports.swarm().naming.lane_of

    by, lane = op["by"], lane_of(op["by"])
    if lane in WORKER_LANES:
        return f"{by} works in the {lane} lane and cannot add tasks: {PROPOSE}"
    if lane != "plan":
        return ""
    plans = [t for t in tasks if (t.get("claimed_by"), t.get("lane"), t.get("state")) == (by, "plan", "claimed")]
    if not plans:
        return "" if op.get("phase") in appended else f"{by} holds no plan task and cannot add tasks: {PROPOSE}"
    if plans[0].get("phase") != op.get("phase"):
        return f"{by} plans phase {plans[0].get('phase')} and cannot add a task outside it: {PROPOSE}"
    return ""


def follow_up_refusal(op: dict) -> str:
    if op.get("follow_up") and (op.get("plan_slice") or op.get("slice")):
        return f"task {op['task']} is a follow up and names no slice: drop --plan-slice or --follow-up"
    return ""


def unsliced_refusal(doc: dict, task: dict, by: str) -> str:
    if task.get("plan_slice") or task.get("slice") or task.get("follow_up") or by == SWARM:
        return ""
    if ledger_kinds.kind(task) == "plan":
        return ""
    from scripts.swarm_ledger import plan_ranges

    phase = next((p for p in doc.get("phases", []) if p["id"] == task.get("phase")), {})
    try:
        names = plan_ranges.anchors(doc, phase)
    except ValueError as exc:
        return str(exc)
    if names:
        return (
            f"phase {phase['id']} has a plan with slice anchors: name the task's slice with --plan-slice "
            f"on task add or plan_slice= on task set, one of {', '.join(names)}"
        )
    return ""


def rank_refusal(by, field="rank"):
    lane_of = ports.swarm().naming.lane_of

    lane = lane_of(by)
    if field == "rank":
        return "" if by in RANK_AUTHORS or lane in RANK_LANES else f"{by} cannot set a task rank: {PROPOSE}"
    if lane in WORKER_LANES:
        return f"{by} works in the {lane} lane and cannot set a task {field}: {PROPOSE}"
    return ""


def update_refusal(doc: dict, op: dict, meta: dict | None = None) -> str:
    if (
        meta is not None
        and op["item"].split("/")[1] in meta.get("outcomes", {})
        and any(key in op["fields"] for key in ("state", "proof", "pr_url", "claimed_by"))
    ):
        return "committed outcomes require controller reconciliation"
    for field in ("rank", "difficulty", "phase"):
        if field in op["fields"] and (refusal := rank_refusal(op["by"], field)):
            return refusal
    if refusal := pinned_refusal(op, meta):
        return refusal
    phase = op["fields"].get("phase")
    if phase is not None and phase not in {p["id"] for p in doc["phases"]}:
        return f"phase {phase} is not on this ledger: name one of its phase ids"
    task = next((t for t in doc["tasks"] if t["id"] == op["item"].split("/")[1]), {})
    return park_refusal(op, task) or urgent_update_refusal(op, task, doc)


def urgent_update_refusal(op: dict, task: dict, doc: dict) -> str:
    fields = op["fields"]
    if "rank" not in fields and "phase" not in fields:
        return ""
    rank = fields.get("rank", task.get("rank"))
    return ledger_rank.urgent_refusal({**task, **fields, "rank": ledger_rank.ALIASES.get(rank, rank)}, doc)


def park_refusal(op: dict, task: dict) -> str:
    after = {**task, **op["fields"]}
    if "parked_on" not in op["fields"] or not after["parked_on"] or (after.get("branch") and after.get("stacked_base")):
        return ""
    if ledger_kinds.read_only(after):
        return ""
    return f"{op['item']} cannot be parked without a branch and a stacked base: park it with agentihooks swarm park"


def pinned_refusal(op: dict, meta: dict | None) -> str:
    if "rank" not in op["fields"]:
        return ""
    stamps = (meta or {}).get("stamps", {})
    return ledger_rank.pinned(op["item"].split("/")[1], stamps.get(f"{op['item']}/rank", {}).get("by"))


def _known(tasks, ids):
    return set(ids) <= {t["id"] for t in tasks}


def next_id(tasks):
    numbers = [int(match.group(1)) for t in tasks if (match := MINTED_RE.fullmatch(t["id"]))]
    return f"t{max(numbers, default=0) + 1}"


def slice_ids(task: dict) -> list[str]:
    return [item.strip() for item in task["proof"]["slice"].split(",")]


def invalid_slice(task: dict, tasks: list[dict]) -> list[str]:
    ids = slice_ids(task)
    known = {item["id"]: item for item in tasks}
    return [
        item or "<empty>"
        for item in ids
        if item not in known
        or known[item].get("phase") != task.get("phase")
        or ledger_kinds.kind(known[item]) == "plan"
    ]


def unlinked_slice(task: dict, tasks: list[dict]) -> list[str]:
    ids = set(slice_ids(task))
    return [item["id"] for item in tasks if item["id"] in ids and not item.get("plan_url")]


def slice_refusal(item: str, plan: dict, doc: dict) -> str:
    from scripts.swarm_ledger import plan_ranges

    if bad := invalid_slice(plan, doc["tasks"]):
        return f"{item} has invalid slice task ids: {', '.join(bad)}"
    if unlinked := unlinked_slice(plan, doc["tasks"]):
        return f"{item} slice tasks carry no plan link: {', '.join(unlinked)}. {PUBLISH}"
    if incomplete := plan_ranges.invalid_tasks(plan, doc, slice_ids(plan)):
        return f"{item} slice tasks lack valid plan ranges or anchors: {', '.join(incomplete)}"
    return ""


def _update_fields(task: dict, fields: dict) -> dict:
    if "contract" in fields:
        fields = {**fields, "contract": {**(task.get("contract") or {}), **fields["contract"]}}
    if ledger_kinds.kind(task) == "plan" and fields.get("kind", "plan") != "plan" and "lane" not in fields:
        return {**fields, "lane": "eng"}
    return fields


def _block_author(by: str) -> bool:
    lane_of = ports.swarm().naming.lane_of

    return by == "operator" or lane_of(by) == "master"


def _held_block(task: dict, op: dict, ctx) -> bool:
    return (
        task.get("state") == "blocked"
        and "state" in op["fields"]
        and not _block_author(op["by"])
        and _block_author(ledger_rank.author(ctx, f"{op['item']}/state"))
    )


def _record_timing(task, changed, op, ctx):
    if "state" in changed:
        extra = {"pr_url": task.get("pr_url", ""), "branch": task.get("branch", "")} if changed["state"] == "pr" else {}
        ctx.record(op["by"], f"task {changed['state']}", op["item"], **extra)
    elif "pr_url" in changed and task.get("state") == "pr":
        ctx.record(op["by"], "task pr", op["item"], pr_url=task["pr_url"], branch=task.get("branch", ""))
    if "parked_on" in changed:
        ctx.record(op["by"], "task parked" if changed["parked_on"] else "task unparked", op["item"])


def _normalized(op):
    if "rank" in op["fields"]:
        op = {**op, "fields": {**op["fields"], "rank": ledger_rank.canonical(op["fields"]["rank"])}}
    if "difficulty" in op["fields"]:
        op = {**op, "fields": sized(op["fields"])}
    return op


def _done_refusal(doc, op, after):
    if after.get("state") != "done":
        return None
    if ledger_kinds.unmet(after):
        return f"{op['item']} cannot be done without its proof: {', '.join(ledger_kinds.unmet(after))}"
    if ledger_kinds.kind(after) == "plan":
        return slice_refusal(op["item"], after, doc)
    return None


def _apply_update(task, after, changed, op, ctx):
    if "kind" in changed and after.get("workspace"):
        from scripts.swarm_ledger import ledger_workspace

        ledger_workspace.rewrite(after)
    if "phase" in changed:
        ctx.record(op["by"], "task moved", op["item"], text=f"{task.get('phase') or 'no phase'} to {changed['phase']}")
    task.update(changed)
    if "state" in changed:
        task["done"] = changed["state"] == "done"
    _record_timing(task, changed, op, ctx)
    for key in changed:
        ctx.stamp(f"{op['item']}/{key}", op["by"])
    ctx.dirty = ctx.dirty or bool(changed)


def _gated(doc, op, ctx, task):
    others = [t for t in doc["tasks"] if t["id"] != op["item"].split("/")[1]]
    if task is None or not _known(others, op["fields"].get("depends_on", []) + op["fields"].get("parked_on", [])):
        return False
    if _held_block(task, op, ctx) or _guarded(task, op) or task_dependencies.held(task, op, doc["tasks"]):
        return True
    if refusal := update_refusal(doc, op, ctx.meta):
        ctx.refused.append(refusal)
        return False
    return None


def _update(doc, op, ctx):
    task_id = op["item"].split("/")[1]
    task = next((t for t in doc.get("tasks", []) if t["id"] == task_id), None)
    if (gated := _gated(doc, op, ctx, task)) is not None:
        return gated
    op = _normalized(op)
    fields = _update_fields(task, op["fields"])
    after = {**task, **fields}
    check_lane(after)
    if refusal := _done_refusal(doc, op, after):
        ctx.refused.append(refusal)
        return False
    _apply_update(task, after, _changed(task, fields, op, ctx), op, ctx)
    return True


def _relay_rank(doc, op, ctx):
    from scripts.swarm_ledger.ledger_relay import RELAYED_FROM, relayed

    task = next((t for t in doc["tasks"] if t["id"] == op["item"].split("/")[1]), None)
    rank, words = ledger_rank.canonical(op["fields"]["rank"]), relayed(op, ctx.meta)
    if refusal := _relay_refusal(task, op, ctx.meta, rank, words):
        ctx.refused.append(refusal)
        return False
    if refusal := ledger_rank.urgent_refusal({**task, "rank": rank}, doc):
        ctx.refused.append(refusal)
        return False
    task["rank"] = rank
    ctx.stamp(f"{op['item']}/rank", ledger_rank.OPERATOR)
    marks = {"relayed_by": op["by"], "relayed_from": RELAYED_FROM, "quote": words}
    ctx.record(ledger_rank.OPERATOR, "rank set", op["item"], text=rank, **marks)
    return True


def _changed(task, fields, op, ctx):
    changed = {k: v for k, v in fields.items() if task.get(k) != v}
    if "claimed_by" in changed:
        changed["claim_generation"] = task.get("claim_generation", 0) + 1
    if "rank" in fields and ledger_rank.author(ctx, f"{op['item']}/rank") != op["by"]:
        changed["rank"] = fields["rank"]
    if fields.get("state") == "blocked" and _block_author(op["by"]):
        changed["state"] = "blocked"
    return changed


def _relay_refusal(task, op, meta, rank, words):
    if task is None:
        return UNRELAYED_TASK
    if meta["members"].get(op["by"], {}).get("role") != "orchestrator":
        return UNRELAYED_ROLE.format(by=op["by"])
    if not words:
        return UNRELAYED_WORDS
    if not ledger_rank.quoted(rank, words):
        return UNRELAYED_RANK.format(rank=rank)
    return ""


def complete_outcome(doc: dict, op: dict, ctx: object, outcome: dict, actor: str) -> bool:
    from scripts.swarm_ledger.api.resources import revision

    proposal = outcome["proposal"]
    task_id = proposal["task_id"]
    task = next((row for row in doc["tasks"] if row["id"] == task_id), None)
    receipt = {
        "operation_id": outcome["operation_id"],
        "digest": revision(
            {
                "provider_id": outcome["provider_id"],
                "task_id": task_id,
                "pr_url": proposal["pr_url"],
                "head_sha": proposal["head_sha"],
                "proof": proposal["proof"],
                "merge_sha": outcome["merge_sha"],
            }
        ),
    }
    known = ctx.meta.get("outcomes", {}).get(task_id)
    if (
        task is None
        or op["op"] != "task_update"
        or op["item"] != f"tasks/{task_id}"
        or op["by"] != actor
        or op["fields"] != {"state": "done", "pr_url": proposal["pr_url"], "proof": proposal["proof"]}
    ):
        ctx.refused.append("outcome identity conflict")
        return False
    if known:
        if known == receipt and task.get("state") == "done" and task.get("pr_url") == proposal["pr_url"]:
            return True
        ctx.refused.append("outcome receipt conflict")
        return False
    if (
        revision(task) != proposal["task_revision"]
        or task.get("claimed_by") != actor
        or task.get("pr_url") != proposal["pr_url"]
        or outcome["phase"] != "externally_verified"
        or not outcome.get("merge_sha")
    ):
        ctx.refused.append("outcome revision or ownership conflict")
        return False
    if not _update(doc, op, ctx):
        return False
    ctx.meta.setdefault("outcomes", {})[task_id] = receipt
    ctx.dirty = True
    return True


def _move_plan(doc: dict, task: dict, fields: dict, phase: dict) -> None:
    if "phase" not in fields or fields["phase"] == task.get("phase"):
        return
    fields["plan_lines"] = ""
    source = next((p for p in doc["phases"] if p["id"] == task.get("phase")), {})
    if "plan_url" not in fields and (fields.get("plan_slice") or task.get("plan_url") == source.get("plan_url")):
        fields["plan_url"] = phase.get("plan_url", "") if fields.get("plan_slice") else ""
    if fields.get("plan_slice") or fields.get("slice"):
        fields.setdefault("follow_up", False)
    elif phase.get("plan_ref") or phase.get("plan_url"):
        fields.setdefault("follow_up", True)
    fields.setdefault("plan_slice", "")
    if task.get("slice"):
        fields.setdefault("slice", "")


def _follow_up(op: dict, ctx) -> bool:
    fields = op["fields"]
    if fields.get("plan_slice") or fields.get("slice"):
        task_id = op["item"].split("/")[1]
        ctx.refused.append(f"task {task_id} is a follow up and names no slice: drop plan_slice or follow_up")
        return False
    fields.update(plan_slice="", plan_lines="", slice="")
    return True


def _set_slice(doc: dict, op: dict, ctx) -> bool:
    fields = op["fields"]
    supplied_slice = "plan_slice" in fields
    task_id = op["item"].split("/")[1]
    task = next((t for t in doc.get("tasks", []) if t["id"] == task_id), None)
    if task is None or (op.get("if_state") and task.get("state", "open") not in op["if_state"]):
        return True
    target = fields.get("phase", task.get("phase"))
    phase = next((p for p in doc.get("phases", []) if p["id"] == target), {})
    _move_plan(doc, task, fields, phase)
    if fields.get("follow_up"):
        return _follow_up(op, ctx)
    if not supplied_slice:
        return True
    from scripts.swarm_ledger import plan_ranges

    try:
        fields["plan_lines"] = plan_ranges.task_slice(
            doc, phase, fields["plan_slice"], fields.get("plan_url", task.get("plan_url", ""))
        )
    except ValueError as exc:
        ctx.refused.append(str(exc))
        return False
    if fields["plan_slice"] != task.get("plan_slice") and (refusal := shared_refusal(doc, {**task, **fields})):
        ctx.refused.append(refusal)
        return False
    return True


def shared_refusal(doc: dict, task: dict) -> str:
    from scripts.swarm_ledger import plan_read

    if not task.get("plan_slice") or not (others := plan_read.sharers(doc, task)):
        return ""
    return (
        f"plan slice {task['plan_slice']} already belongs to open task {', '.join(others)}: give each task its own "
        "slice anchor, a <!-- slice: name --> line above its own row or section, or group the tasks into one grouped task"
    )


def apply(doc, op, ctx):
    if op["op"] == "task_add":
        return _add(doc, op, ctx)
    if "quote" in op:
        return _relay_rank(doc, op, ctx)
    if op.get("if_plan_lines_missing") and any(
        t["id"] == op["item"].split("/")[1] and t.get("plan_lines") for t in doc.get("tasks", [])
    ):
        return True
    op = {**op, "fields": ledger_plans.with_plan_slice(doc, op["fields"])}
    if "plan_slice" not in op["fields"]:
        op = {**op, "fields": dict(op["fields"])}
    if not _set_slice(doc, op, ctx):
        return False
    if refusal := _parent_refusal(doc, op):
        ctx.refused.append(refusal)
        return False
    return _update(doc, op, ctx)


def _parent_refusal(doc: dict, op: dict) -> str:
    task = next((t for t in doc["tasks"] if t["id"] == op["item"].split("/")[1]), None)
    if task is None or not PARENT_FIELDS & set(op["fields"]):
        return ""
    return ledger_plans.task_refusal(doc, {**task, **op["fields"]})
