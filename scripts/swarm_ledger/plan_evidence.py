import json
import os
from collections.abc import Callable
from typing import Any, cast


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


def _phase_plan(phase: dict[str, Any], doc: dict[str, Any], limit: int) -> list[str]:
    ref: dict[str, str] | None = phase.get("plan_ref")
    if not ref:
        return []
    from scripts.swarm_ledger import plan_ranges, plan_read

    pointer = f"Read the whole phase plan with {plan_read.COMMAND} --phase {phase['id']}."
    try:
        stored = cast(Callable[[dict[str, str], dict[str, Any]], str], plan_ranges.stored_text)
        text = stored(ref, doc)
    except (ValueError, OSError) as exc:
        return [f"Phase plan\n{pointer}\nIt cannot be read here: {exc}"]
    names = plan_ranges.slice_anchors(text, ref["lines"])
    listed = (
        "The phase plan is already published. Add one task per slice with task add --plan-slice <name> and do not "
        f"publish it again: {', '.join(names)}"
        if names
        else "None: the published phase plan holds no slice anchors, so write the slice as a plan with anchors and "
        "publish it before adding tasks."
    )
    return [
        _bounded("Phase plan", f"{pointer}\n{plan_read.chunk(text, ref['lines'], margin=0)}", limit),
        _bounded("Slice names", listed, limit),
    ]


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
    lines.extend(_phase_plan(cast(dict[str, Any], phase), cast(dict[str, Any], doc), limit))
    return "\n\n".join(lines) + "\n"
