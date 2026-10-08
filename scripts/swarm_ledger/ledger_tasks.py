"""Swarm tasks: claimable units of work under a phase, each in the eng or ci lane."""

import re

import ledger_comments
import ledger_kinds

from scripts.swarm_ledger import ledger_rank

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
    "rank",
    "branch",
    "branch_repo",
    "stacked_base",
    "parked_on",
    "parked_repos",
    "overlays",
    "difficulty",
    "difficulty_source",
    "difficulty_confidence",
)
BOOL_FIELDS = ("artifact",)
DIFFICULTIES = ("S", "M", "L")
DIFFICULTY_SOURCES = ("operator", "rule", "classifier", "default")
DIFFICULTY_FIELDS = ("difficulty", "difficulty_source", "difficulty_confidence")
NUMBER_FIELDS = ("difficulty_confidence",)
LIST_FIELDS = ("depends_on", "territory", "parked_on", "parked_repos", "overlays")
OVERLAY_CAP = 3
BRANCH_RE = re.compile(r"^(?!-)\S*$")
COMMIT_RE = re.compile(r"^([0-9a-f]{7,64})?$")
OBJECT_FIELDS = ("contract", "proof")
URL_FIELDS = ("issue_url", "pr_url", "plan_url")
URL_RE = re.compile(r"^https?://[^\s]+$")
OPS = ("task_add", "task_update")
WORKER_LANES = ("eng", "ci")
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
        if not all(isinstance(op.get(key, ""), str) for key in ("phase", "description", "workspace")):
            raise ValueError("phase, description and workspace must be strings")
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
    guard = op.get("if_state", [])
    if not isinstance(guard, list) or not all(state in STATES for state in guard):
        raise ValueError(f"if_state must be a list of states from {STATES}")
    check_lists(fields)
    check_stack(fields)
    check_bools(fields)
    check_profile(fields)
    check_urls(fields)
    check_rank(fields)
    ledger_kinds.check(fields)


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


def check_lists(fields):
    for key in LIST_FIELDS:
        value = fields.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise ValueError(f"{key} must be a list of nonempty strings")
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
    check_urls(task)
    check_profile(task)
    check_rank(task)
    check_difficulty(task)
    ledger_kinds.check(task)
    check_lane(task)
    if task.get("state") == "done" and ledger_kinds.unmet(task):
        raise ValueError(f"tasks/{task.get('id')} is done without its proof: {', '.join(ledger_kinds.unmet(task))}")


def _add(doc, op, ctx):
    tasks = doc.setdefault("tasks", [])
    if taken := next((t for t in tasks if t["id"] == op["task"]), None):
        ctx.refused.append(
            f'task {taken["id"]} already exists as "{taken["title"]}" ({taken.get("state", "open")}): '
            "pick another id, or pass - as the id to mint one"
        )
        return False
    if not _known(tasks, op.get("depends_on", [])):
        return False
    if refusal := add_refusal(tasks, op):
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
    for key in ("gain", "contract", "workspace", "artifact", "profile", "overlays"):
        if key in op:
            task[key] = op[key]
    if "rank" in op:
        task["rank"] = ledger_rank.canonical(op["rank"])
    task.update({k: v for k, v in sized(op).items() if k in DIFFICULTY_FIELDS})
    phase = next((p for p in doc.get("phases", []) if p["id"] == task["phase"]), {})
    if plan_url := op.get("plan_url") or phase.get("plan_url"):
        task["plan_url"] = plan_url
    tasks.append(task)
    ctx.record(op["by"], "added", f"tasks/{task['id']}", text=task["title"])
    return True


def add_refusal(tasks, op):
    from scripts.swarm.naming import lane_of

    by, lane = op["by"], lane_of(op["by"])
    if lane in WORKER_LANES:
        return f"{by} works in the {lane} lane and cannot add tasks: {PROPOSE}"
    if lane != "plan":
        return ""
    plans = [t for t in tasks if (t.get("claimed_by"), t.get("lane"), t.get("state")) == (by, "plan", "claimed")]
    if not plans:
        return f"{by} holds no plan task and cannot add tasks: {PROPOSE}"
    if plans[0].get("phase") != op.get("phase"):
        return f"{by} plans phase {plans[0].get('phase')} and cannot add a task outside it: {PROPOSE}"
    return ""


def rank_refusal(by, field="rank"):
    from scripts.swarm.naming import lane_of

    lane = lane_of(by)
    if lane in WORKER_LANES:
        return f"{by} works in the {lane} lane and cannot set a task {field}: {PROPOSE}"
    return ""


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


def _update_fields(task: dict, fields: dict) -> dict:
    if ledger_kinds.kind(task) == "plan" and fields.get("kind", "plan") != "plan" and "lane" not in fields:
        return {**fields, "lane": "eng"}
    return fields


def _update(doc, op, ctx):
    task_id = op["item"].split("/")[1]
    task = next((t for t in doc.get("tasks", []) if t["id"] == task_id), None)
    others = [t for t in doc["tasks"] if t["id"] != task_id]
    if task is None or not _known(others, op["fields"].get("depends_on", []) + op["fields"].get("parked_on", [])):
        return False
    if op.get("if_state") and task.get("state", "open") not in op["if_state"]:
        return True
    if "rank" in op["fields"]:
        if refusal := rank_refusal(op["by"]):
            ctx.refused.append(refusal)
            return False
        op = {**op, "fields": {**op["fields"], "rank": ledger_rank.canonical(op["fields"]["rank"])}}
    if "difficulty" in op["fields"]:
        if refusal := rank_refusal(op["by"], "difficulty"):
            ctx.refused.append(refusal)
            return False
        op = {**op, "fields": sized(op["fields"])}
    fields = _update_fields(task, op["fields"])
    after = {**task, **fields}
    check_lane(after)
    if after.get("state") == "done" and ledger_kinds.unmet(after):
        ctx.refused.append(f"{op['item']} cannot be done without its proof: {', '.join(ledger_kinds.unmet(after))}")
        return False
    if after.get("state") == "done" and ledger_kinds.kind(after) == "plan":
        bad = invalid_slice(after, doc["tasks"])
        if bad:
            ctx.refused.append(f"{op['item']} has invalid slice task ids: {', '.join(bad)}")
            return False
        if unlinked := unlinked_slice(after, doc["tasks"]):
            ctx.refused.append(f"{op['item']} slice tasks carry no plan link: {', '.join(unlinked)}. {PUBLISH}")
            return False
    changed = {k: v for k, v in fields.items() if task.get(k) != v}
    if "kind" in changed and after.get("workspace"):
        from scripts.swarm_ledger import ledger_workspace

        ledger_workspace.rewrite(after)
    task.update(changed)
    if "state" in changed:
        task["done"] = changed["state"] == "done"
        ctx.record(op["by"], f"task {changed['state']}", op["item"])
    for key in changed:
        ctx.stamp(f"{op['item']}/{key}", op["by"])
    ctx.dirty = ctx.dirty or bool(changed)
    return True


def apply(doc, op, ctx):
    return _add(doc, op, ctx) if op["op"] == "task_add" else _update(doc, op, ctx)
