import re

OPS = ("phase_add", "phase_update", "phase_review", "phase_append")
FIELDS = ("title", "description", "depends_on", "planning", "release", "plan_url", "plan_ref")
URL_RE = re.compile(r"^https?://[^\s]+$")
ID_RE = re.compile(r"^[A-Za-z][\w.-]{0,63}$")
AUTHOR_RE = re.compile(r"^[A-Za-z][\w.@-]{0,63}$")
ITEM_RE = re.compile(r"^phases/[A-Za-z][\w.-]{0,63}$")
REVIEW_STATES = ("pending", "approved", "sent_back")
ROUND_CAP = 3
PLANNING_DEFAULT = "auto"


def check_fields(fields: dict) -> None:
    from scripts.swarm_ledger import plan_ranges

    if "plan_ref" in fields:
        plan_ranges.check_ref(fields["plan_ref"])
    for key in ("title", "description"):
        if key in fields and not isinstance(fields[key], str):
            raise ValueError(f"{key} must be a string")
    if "depends_on" in fields and (
        not isinstance(fields["depends_on"], list)
        or not all(isinstance(value, str) and value for value in fields["depends_on"])
    ):
        raise ValueError("depends_on must be a list of nonempty phase ids")
    if "planning" in fields and fields["planning"] not in ("manual", "auto"):
        raise ValueError("planning must be manual or auto")
    if "release" in fields and type(fields["release"]) is not bool:
        raise ValueError("release must be a boolean")
    if "plan_url" in fields and not (isinstance(fields["plan_url"], str) and URL_RE.match(fields["plan_url"])):
        raise ValueError("plan_url must be an http or https link")


def validate(phases: list[dict]) -> None:
    graph = {phase["id"]: phase.get("depends_on", []) for phase in phases}
    for phase in phases:
        check_fields(phase)
        unknown = [value for value in phase.get("depends_on", []) if value not in graph]
        if unknown:
            raise ValueError(f"phase {phase['id']} depends on unknown phases: {', '.join(unknown)}")
    visited = set()
    for phase in graph:
        _visit(phase, graph, [], visited)


def _visit(phase: str, graph: dict, chain: list[str], visited: set) -> None:
    if phase in chain:
        raise ValueError(f"phase dependency cycle: {' -> '.join(chain[chain.index(phase) :] + [phase])}")
    if phase in visited:
        return
    for dependency in graph[phase]:
        _visit(dependency, graph, [*chain, phase], visited)
    visited.add(phase)


def seed_graph_valid(phases: list[dict], base: list[dict], seed: list[dict], ctx) -> bool:
    previous = {phase["id"]: phase for phase in base}
    candidate = {phase["id"]: phase.copy() for phase in phases}
    for phase in seed:
        phase_id = phase["id"]
        if phase_id not in candidate and phase_id not in previous:
            candidate[phase_id] = phase.copy()
        elif phase_id in candidate:
            for key in FIELDS:
                if key in phase and phase.get(key) != previous.get(phase_id, {}).get(key):
                    candidate[phase_id][key] = phase[key]
    try:
        validate(list(candidate.values()))
    except ValueError as exc:
        ctx.refused.append(str(exc))
        return False
    return True


def check(op: dict) -> None:
    by = op.get("by")
    if not isinstance(by, str) or not AUTHOR_RE.fullmatch(by):
        raise ValueError("phase ops need by, an agent name or operator")
    if op["op"] == "phase_append":
        return check_append(op)
    if op["op"] == "phase_add":
        if not isinstance(op.get("phase"), str) or not ID_RE.fullmatch(op["phase"]):
            raise ValueError("phase_add needs a phase id")
        if not isinstance(op.get("title"), str) or not op["title"].strip():
            raise ValueError("phase_add needs a title")
        fields = {k: v for k, v in op.items() if k not in ("op", "id", "by", "phase")}
    else:
        if not ITEM_RE.fullmatch(str(op.get("item"))):
            raise ValueError("phase ops need item phases/<id>")
        if op["op"] == "phase_review":
            return check_review(op)
        fields = op.get("fields")
    if not isinstance(fields, dict) or not fields or set(fields) - set(FIELDS):
        raise ValueError(f"phase fields may set only {FIELDS}")
    check_fields(fields)


def check_review(op: dict) -> None:
    if set(op) - {"op", "id", "by", "item", "state", "rounds", "note", "escalated", "override"}:
        raise ValueError("phase_review writes only the review record")
    if op.get("state") not in REVIEW_STATES:
        raise ValueError(f"review state must be one of {REVIEW_STATES}")
    if type(op.get("rounds", 0)) is not int or op.get("rounds", 0) < 0:
        raise ValueError("review rounds must be a nonnegative integer")
    if not isinstance(op.get("note", ""), str):
        raise ValueError("review note must be a string")
    if "escalated" in op and type(op["escalated"]) is not bool:
        raise ValueError("review escalated must be a boolean")
    if op["state"] == "sent_back" and not op.get("note", "").strip():
        raise ValueError("a send back needs a note")
    if op["state"] == "sent_back" and ("rounds" in op or "escalated" in op):
        raise ValueError("a send back counts its own rounds")
    if "override" in op:
        check_override(op)


def check_override(op: dict) -> None:
    if op["state"] != "approved":
        raise ValueError("only an approval carries an override")
    override = op["override"]
    malformed = ValueError("an override needs a reason and the problems it overrode")
    if not isinstance(override, dict) or set(override) != {"reason", "problems"}:
        raise malformed
    reason, problems = override["reason"], override["problems"]
    if not isinstance(reason, str) or not reason.strip() or not isinstance(problems, list) or not problems:
        raise malformed
    if not all(isinstance(problem, str) for problem in problems):
        raise malformed


def check_append(op: dict) -> None:
    phases = op.get("phases")
    if not isinstance(phases, list) or not phases:
        raise ValueError("phase_append needs a list of phases")
    seen = set()
    for entry in phases:
        if not isinstance(entry, dict):
            raise ValueError("each appended phase is an object")
        check({**entry, "op": "phase_add", "by": op["by"]})
        if entry["phase"] in seen:
            raise ValueError(f"phase {entry['phase']} appears twice in the plan")
        seen.add(entry["phase"])


def append(doc: dict, op: dict, ctx) -> bool:
    ids = {phase["id"] for phase in doc["phases"]}
    taken = [f"phase id {entry['phase']} is already taken" for entry in op["phases"] if entry["phase"] in ids]
    if taken:
        ctx.refused.extend(taken)
        return False
    review = {"state": "pending", "by": op["by"], "at": ctx.at, "rounds": 0, "note": ""}
    added = [
        {
            "id": entry["phase"],
            "description": "",
            "done": False,
            **{k: entry[k] for k in FIELDS if k in entry},
            "planning": entry.get("planning", PLANNING_DEFAULT),
            "added_by": op["by"],
            **({"review": dict(review)} if entry.get("planning") == "manual" else {}),
        }
        for entry in op["phases"]
    ]
    try:
        validate(doc["phases"] + added)
    except ValueError as exc:
        ctx.refused.append(str(exc))
        return False
    for phase in added:
        doc["phases"].append(phase)
        ctx.record(op["by"], "added", f"phases/{phase['id']}", text=phase["title"])
    return True


def apply(doc: dict, op: dict, ctx) -> bool:
    if op["op"] == "phase_append":
        return append(doc, op, ctx)
    phases = doc["phases"]
    phase_id = op["phase"] if op["op"] == "phase_add" else op["item"].split("/")[1]
    phase = next((phase for phase in phases if phase["id"] == phase_id), None)
    if op["op"] == "phase_add" and phase is not None:
        return True
    if op["op"] != "phase_add" and phase is None:
        return False
    if op["op"] == "phase_review":
        fields = {"review": review_record(phase, op, ctx.at)}
    else:
        fields = (
            {"planning": PLANNING_DEFAULT, **{k: op[k] for k in FIELDS if k in op}} if phase is None else op["fields"]
        )
    after = {**(phase or {"id": phase_id, "description": "", "done": False, "comments": []}), **fields}
    try:
        validate([after if p["id"] == phase_id else p for p in phases] + ([after] if phase is None else []))
        if "plan_ref" in fields:
            from scripts.swarm_ledger import plan_ranges

            plan_ranges.check_phase_ref(doc, after)
    except ValueError as exc:
        ctx.refused.append(str(exc))
        return False
    target = f"phases/{phase_id}"
    if phase is None:
        phases.append(after)
        ctx.record(op["by"], "added", target, text=after["title"])
    else:
        changed = {k: v for k, v in fields.items() if phase.get(k) != v}
        phase.update(changed)
        for key in changed:
            ctx.stamp(f"{target}/{key}", op["by"])
            ctx.record(op["by"], f"{key} changed", target)
    if op["op"] == "phase_review":
        decided(doc, phase_id, fields["review"], op["by"], ctx)
    return True


def review_record(phase: dict, op: dict, at: int) -> dict:
    prev = phase.get("review") or {}
    record = {"state": op["state"], "by": op["by"], "at": at, "rounds": op.get("rounds", prev.get("rounds", 0))}
    record["note"] = op.get("note", "")
    if prev.get("notes"):
        record["notes"] = prev["notes"]
    if "escalated" in op:
        record["escalated"] = op["escalated"]
    if "override" in op:
        record["override"] = op["override"]
    if op["state"] == "sent_back":
        record["rounds"] = prev.get("rounds", 0) + 1
        record["notes"] = [*prev.get("notes", []), op["note"]]
        record["escalated"] = not prev.get("escalated") and record["rounds"] >= ROUND_CAP
    return record


def decided(doc: dict, phase_id: str, review: dict, by: str, ctx) -> None:
    if review["state"] == "pending":
        return
    item = f"phases/{phase_id}"
    doc["priorities"] = [row for row in doc["priorities"] if row["item"] != item]
    if review["state"] != "sent_back" or review["escalated"]:
        return
    plan = next((t for t in doc["tasks"] if t.get("phase") == phase_id and t.get("kind") == "plan"), None)
    if plan is None or plan.get("state") == "open":
        return
    plan.update(state="open", claimed_by="", done=False)
    ctx.stamp(f"tasks/{plan['id']}/state", by)
    ctx.record(by, "task open", f"tasks/{plan['id']}")
