import json
import os


def _dependencies(phase, phases):
    pending = list(phase.get("depends_on", []))
    seen = {phase["id"]}
    while pending:
        phase_id = pending.pop(0)
        if phase_id in seen:
            continue
        seen.add(phase_id)
        dependency = phases[phase_id]
        yield dependency
        pending.extend(dependency.get("depends_on", []))


def _bounded(title, text, limit):
    if len(text) <= limit:
        return f"{title}\n{text}"
    return f"{title}\n{text[:limit]}\n[Cut {title}: {len(text) - limit} characters omitted]"


def evidence(task: dict, doc: dict) -> str:
    limit = int(os.environ.get("AGENTIHOOKS_PLAN_EVIDENCE_CHARS", "6000"))
    phases = {phase["id"]: phase for phase in doc["phases"]}
    phase = phases[task["phase"]]
    lines = [
        "Plan evidence",
        _bounded("Project intent", doc["overview"], limit),
        _bounded("Mission intent", f"{phase['title']}\n{phase.get('description', '')}", limit),
    ]
    for dependency in _dependencies(phase, phases):
        title = dependency["title"]
        tasks = [
            {key: row.get(key, "") for key in ("title", "state", "pr_url", "proof")}
            for row in doc["tasks"]
            if row.get("phase") == dependency["id"]
        ]
        lines.append(_bounded(f"Dependency tasks for {title}", json.dumps(tasks, ensure_ascii=False), limit))
        comments = [comment["text"] for comment in dependency.get("comments", []) if not comment.get("deleted")][-5:]
        lines.append(_bounded(f"Last five comments for {title}", "\n".join(comments), limit))
    review = phase.get("review") or {}
    if review.get("state") == "sent_back":
        lines.append(_bounded("Review note", review.get("note", ""), limit))
    return "\n\n".join(lines) + "\n"
