"""Plans and slices: the ledger resources a phase and a task name as their parents."""

import re

OPS = ("plan_add", "slice_add")
PLAN_RE = re.compile(r"^[A-Za-z][\w-]{0,63}$")
ANCHOR_RE = re.compile(r"^[\w.-]{1,64}$")
ADDRESS_RE = re.compile(r"^(?:ledgers/(?P<ledger>[^/\s]+)/)?(?P<kind>[^/\s]+)/(?P<id>[^/\s]+)$")
AUTHOR_RE = re.compile(r"^[A-Za-z][\w.@-]{0,63}$")
URL_RE = re.compile(r"^https?://[^\s]+$")
LINKS = ("artifact", "url")
NOUNS = {"plans": "plan", "phases": "phase", "slices": "slice"}
PARENTS = (("phases", "plan"), ("tasks", "slice"))


def check(op: dict) -> None:
    if not isinstance(op.get("by"), str) or not AUTHOR_RE.fullmatch(op["by"]):
        raise ValueError(f"{op['op']} needs by, an agent name or operator")
    if op["op"] == "slice_add":
        if set(op) - {"op", "id", "by", "phase", "anchor"}:
            raise ValueError("slice_add takes only phase anchor")
        anchor = op.get("anchor")
        if not isinstance(op.get("phase"), str) or not isinstance(anchor, str) or not ANCHOR_RE.fullmatch(anchor):
            raise ValueError("slice_add needs phase and an anchor")
        return
    if set(op) - {"op", "id", "by", "plan", "title", *LINKS}:
        raise ValueError("plan_add takes only plan title artifact url")
    if not isinstance(op.get("plan"), str) or not PLAN_RE.fullmatch(op["plan"]):
        raise ValueError("plan_add needs plan, an id of letters, digits, _ and -")
    if not isinstance(op.get("title"), str) or not op["title"].strip():
        raise ValueError("plan_add needs a title")
    check_links(op)


def check_links(fields: dict) -> None:
    for key in LINKS:
        value = fields.get(key, "")
        if not isinstance(value, str) or value and not URL_RE.fullmatch(value):
            raise ValueError(f"{key} must be an http or https link")


def validate(doc: dict) -> None:
    for name in ("plans", "slices"):
        items = doc.get(name, [])
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValueError(f"{name} must be a list of objects")
        ids = [item.get("id") for item in items]
        if not all(isinstance(i, str) and i for i in ids) or len(set(ids)) != len(ids):
            raise ValueError(f"every {name} item needs a unique id")
    for plan in doc.get("plans", []):
        check_links(plan)
    for name, field in PARENTS:
        for item in doc.get(name, []):
            if not isinstance(item.get(field, ""), str):
                raise ValueError(f"{name}/{item['id']}/{field} must be str")
    refusals = [
        *(slice_refusal(doc, row) for row in doc.get("slices", [])),
        *(phase_refusal(doc, row) for row in doc.get("phases", [])),
        *(task_refusal(doc, row) for row in doc.get("tasks", [])),
    ]
    if refusal := next((text for text in refusals if text), ""):
        raise ValueError(refusal)


def find(doc: dict, address: str) -> dict:
    kind, item = address.split("/", 1)
    return next(row for row in doc.get(kind, []) if row.get("id") == item)


def parent_refusal(doc: dict, owner: str, address: object, kind: str) -> str:
    match = ADDRESS_RE.fullmatch(address) if isinstance(address, str) else None
    if match and match["ledger"]:
        where = f"{owner} names {address} on ledger {match['ledger']}"
        return f"{where}: a parent lives on the same ledger, named as {kind}/<id>"
    if match is None or match["kind"] != kind:
        return f"{owner} needs a {NOUNS[kind]} as its parent, not {address}"
    if not any(row.get("id") == match["id"] for row in doc.get(kind, [])):
        return f"{owner} names an unknown {NOUNS[kind]} {address}"
    return ""


def slice_refusal(doc: dict, row: dict) -> str:
    return parent_refusal(doc, f"slice {row.get('anchor')}", row.get("phase"), "phases")


def phase_refusal(doc: dict, phase: dict) -> str:
    address = phase.get("plan")
    if not address:
        return ""
    owner = f"phase {phase['id']}"
    if refusal := parent_refusal(doc, owner, address, "plans"):
        return refusal
    prefix = f"{address.split('/', 1)[1]}."
    if stale := [
        row["id"]
        for row in doc.get("slices", [])
        if row.get("phase") == f"phases/{phase['id']}" and not row["id"].startswith(prefix)
    ]:
        return f"{owner} holds slices of another plan: {', '.join(stale)}"
    plan = find(doc, address)
    links = {plan.get(key) for key in LINKS} - {"", None}
    legacy = (("plan_url", phase.get("plan_url")), ("plan_ref", (phase.get("plan_ref") or {}).get("artifact")))
    for key, link in legacy:
        if link and links and link not in links:
            return f"{owner} {key} {link} is not a link of {address}"
    return ""


def task_refusal(doc: dict, task: dict) -> str:
    address = task.get("slice")
    if not address:
        return ""
    owner = f"task {task['id']}"
    if refusal := parent_refusal(doc, owner, address, "slices"):
        return refusal
    row = find(doc, address)
    if row.get("phase") != f"phases/{task.get('phase', '')}":
        return (
            f"{owner} is in phase {task.get('phase') or 'none'} but its slice {address} belongs to {row.get('phase')}"
        )
    if task.get("plan_slice") and task["plan_slice"] != row.get("anchor"):
        return f"{owner} plan_slice {task['plan_slice']} differs from its slice anchor {row.get('anchor')}"
    if task.get("plan_lines") and row.get("lines") and task["plan_lines"] != row["lines"]:
        return f"{owner} plan_lines {task['plan_lines']} differ from its slice lines {row['lines']}"
    return ""


def apply(doc: dict, op: dict, ctx) -> bool:
    if op["op"] == "plan_add":
        refusal = add_plan(doc, op, ctx)
    else:
        refusal = add_slice(doc, op, ctx)
    if refusal:
        ctx.refused.append(refusal)
    return not refusal


def add_plan(doc: dict, op: dict, ctx) -> str:
    plans = doc.setdefault("plans", [])
    plan = {"id": op["plan"], "title": op["title"].strip(), **{key: op.get(key, "") for key in LINKS}}
    if existing := next((row for row in plans if row["id"] == plan["id"]), None):
        return "" if existing == plan else f"plan {plan['id']} already exists"
    plans.append(plan)
    ctx.record(op["by"], "added", f"plans/{plan['id']}", text=plan["title"])
    return ""


def add_slice(doc: dict, op: dict, ctx) -> str:
    if refusal := parent_refusal(doc, f"slice {op['anchor']}", op["phase"], "phases"):
        return refusal
    phase = find(doc, op["phase"])
    if not phase.get("plan"):
        return f"phase {phase['id']} has no plan: link it to a plan before adding a slice"
    plan = find(doc, phase["plan"])
    slices = doc.setdefault("slices", [])
    row = {"id": f"{plan['id']}.{op['anchor']}", "phase": op["phase"], "anchor": op["anchor"]}
    if existing := next((item for item in slices if item["id"] == row["id"]), None):
        return "" if existing["phase"] == row["phase"] else f"slice {row['id']} already belongs to {existing['phase']}"
    try:
        row["lines"] = slice_lines(doc, phase, plan, op["anchor"])
    except ValueError as exc:
        return str(exc)
    slices.append(row)
    ctx.record(op["by"], "added", f"slices/{row['id']}", text=op["anchor"])
    return ""


def slice_lines(doc: dict, phase: dict, plan: dict, anchor: str) -> str:
    if not (phase.get("plan_ref") or plan.get("artifact")):
        return ""
    from scripts.swarm_ledger import plan_ranges

    return plan_ranges.task_slice(doc, phase, anchor, plan.get("artifact", ""))
