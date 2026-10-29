"""A phase's lifecycle, derived from stored facts only, so the tick, the CLI and the page cannot drift apart."""

LABELS = {
    "out_of_scope": "Out of scope",
    "done": "Done",
    "to_plan": "Needs a plan",
    "planning": "Planning",
    "in_review": "Plan in review",
    "building": "Building",
}
WAITS = "Waits for phase "


def lifecycle(phase, doc):
    if phase.get("out_of_scope"):
        return "out_of_scope"
    if phase.get("done"):
        return "done"
    done = {p["id"] for p in doc.get("phases", []) if p.get("done")}
    if any(dep not in done for dep in phase.get("depends_on") or []):
        return "waiting"
    if phase.get("planning") != "auto":
        return "building"
    plan = _plan_task(phase, doc)
    if plan is None:
        return "to_plan"
    if plan.get("state") != "done":
        return "planning"
    if (phase.get("review") or {}).get("state") != "approved":
        return "in_review"
    return "building"


def label(phase, doc):
    state = lifecycle(phase, doc)
    if state != "waiting":
        return LABELS[state]
    by_id = {p["id"]: p for p in doc.get("phases", [])}
    waits = [dep for dep in phase["depends_on"] if not by_id.get(dep, {}).get("done")]
    return WAITS + ", ".join(by_id.get(dep, {}).get("title") or dep for dep in waits)


def status(doc):
    return {p["id"]: {"state": lifecycle(p, doc), "label": label(p, doc)} for p in doc.get("phases", [])}


def admits(task, doc):
    phase = next((p for p in doc.get("phases", []) if p["id"] == task.get("phase")), None)
    if phase is None:
        return True
    return lifecycle(phase, doc) == ("planning" if task.get("kind") == "plan" else "building")


def report(doc):
    rows = []
    for phase in doc.get("phases", []):
        held = [
            t["id"]
            for t in doc["tasks"]
            if t.get("phase") == phase["id"]
            and t.get("state") == "open"
            and not t.get("out_of_scope")
            and not admits(t, doc)
        ]
        rows.append((phase["id"], lifecycle(phase, doc), held))
    return rows


def _plan_task(phase, doc):
    return next((t for t in doc["tasks"] if t.get("phase") == phase["id"] and t.get("kind") == "plan"), None)
