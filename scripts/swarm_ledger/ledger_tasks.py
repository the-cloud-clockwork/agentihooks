"""Swarm tasks: claimable units of work under a phase, each in the eng or ci lane."""

import re

import ledger_comments
import ledger_kinds

AUTHOR_RE = re.compile(r"^[A-Za-z][\w.@-]{0,63}$")
ID_RE = re.compile(r"^[A-Za-z0-9][\w.-]{0,63}$")
ITEM_RE = re.compile(r"^tasks/[^/]+$")
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
)
BOOL_FIELDS = ("artifact",)
LIST_FIELDS = ("depends_on", "territory")
OBJECT_FIELDS = ("contract", "proof")
URL_FIELDS = ("issue_url", "pr_url")
URL_RE = re.compile(r"^https?://[^\s]+$")
OPS = ("task_add", "task_update")
WORKER_LANES = ("eng", "ci")
PROPOSE = 'propose the work with agentihooks ledger followup add "<plain words>" and the master decides'


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
    strings = {k: v for k, v in fields.items() if k not in LIST_FIELDS + OBJECT_FIELDS + BOOL_FIELDS}
    if set(fields) - set(UPDATABLE) or not all(isinstance(v, str) for v in strings.values()):
        raise ValueError(
            f"task_update may set only {UPDATABLE}, as strings, {LIST_FIELDS} as lists or {OBJECT_FIELDS} as objects"
        )
    if "state" in fields and fields["state"] not in STATES:
        raise ValueError(f"state must be one of {STATES}")
    check_lists(fields)
    check_bools(fields)
    check_urls(fields)
    ledger_kinds.check(fields)


def check_lists(fields):
    for key in LIST_FIELDS:
        value = fields.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise ValueError(f"{key} must be a list of nonempty strings")


def check_bools(fields):
    for key in BOOL_FIELDS:
        if key in fields and not isinstance(fields[key], bool):
            raise ValueError(f"{key} must be true or false")


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
    check_urls(task)
    ledger_kinds.check(task)
    check_lane(task)
    if task.get("state") == "done" and ledger_kinds.unmet(task):
        raise ValueError(f"tasks/{task.get('id')} is done without its proof: {', '.join(ledger_kinds.unmet(task))}")


def _add(doc, op, ctx):
    tasks = doc.setdefault("tasks", [])
    if any(t["id"] == op["task"] for t in tasks):
        return True
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
    for key in ("gain", "contract", "workspace", "artifact"):
        if key in op:
            task[key] = op[key]
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


def _known(tasks, ids):
    return set(ids) <= {t["id"] for t in tasks}


def invalid_slice(task: dict, tasks: list[dict]) -> list[str]:
    ids = [item.strip() for item in task["proof"]["slice"].split(",")]
    known = {item["id"]: item for item in tasks}
    return [
        item or "<empty>"
        for item in ids
        if item not in known
        or known[item].get("phase") != task.get("phase")
        or ledger_kinds.kind(known[item]) == "plan"
    ]


def _update(doc, op, ctx):
    task_id = op["item"].split("/")[1]
    task = next((t for t in doc.get("tasks", []) if t["id"] == task_id), None)
    others = [t for t in doc["tasks"] if t["id"] != task_id]
    if task is None or not _known(others, op["fields"].get("depends_on", [])):
        return False
    after = {**task, **op["fields"]}
    check_lane(after)
    if after.get("state") == "done" and ledger_kinds.unmet(after):
        ctx.refused.append(f"{op['item']} cannot be done without its proof: {', '.join(ledger_kinds.unmet(after))}")
        return False
    if after.get("state") == "done" and ledger_kinds.kind(after) == "plan":
        bad = invalid_slice(after, doc["tasks"])
        if bad:
            ctx.refused.append(f"{op['item']} has invalid slice task ids: {', '.join(bad)}")
            return False
    changed = {k: v for k, v in op["fields"].items() if task.get(k) != v}
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
