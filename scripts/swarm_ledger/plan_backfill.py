import json

from scripts.swarm_ledger import plan_ranges


def run(args) -> None:
    from scripts.swarm_ledger import ledger

    doc = ledger.call(args.slug)
    report = {"updated": [], "missing": []}
    for task in doc.get("tasks", []):
        if task.get("state") == "done" or task.get("done") or not task.get("plan_url") or task.get("plan_lines"):
            continue
        name = task.get("plan_slice") or task["id"]
        phase = next((p for p in doc.get("phases", []) if p["id"] == task.get("phase")), {})
        try:
            plan_ranges.task_slice(doc, phase, name, task["plan_url"])
            ledger.send(
                args,
                "task_update",
                item=f"tasks/{task['id']}",
                fields={"plan_slice": name},
                if_state=["open", "claimed", "blocked", "pr"],
            )
        except (ValueError, OSError, SystemExit) as exc:
            report["missing"].append({"task": task["id"], "reason": str(exc)})
        else:
            report["updated"].append(task["id"])
    print(json.dumps(report))
