import json

from scripts.swarm_ledger import plan_packages, plan_ranges


def run(args) -> None:
    from scripts.swarm_ledger import ledger

    doc = ledger.call(args.slug)
    report = {"updated": [], "missing": []}
    for task in doc.get("tasks", []):
        if task.get("state") == "done" or task.get("done") or not task.get("plan_url") or task.get("plan_lines"):
            continue
        phase = next((p for p in doc.get("phases", []) if p["id"] == task.get("phase")), {})
        try:
            name = plan_packages.name(task)
            expected = plan_ranges.task_slice(doc, phase, name, task["plan_url"])
            saved = ledger.send(
                args,
                "task_update",
                item=f"tasks/{task['id']}",
                fields={"plan_slice": name},
                if_state=["open", "claimed", "blocked", "pr"],
                if_plan_lines_missing=True,
            )
            row = next((t for t in saved.get("tasks", []) if t["id"] == task["id"]), {})
            if row.get("plan_lines") != expected or row.get("plan_slice") != name:
                raise ValueError("task changed before its plan lines were saved")
        except (ValueError, OSError, SystemExit) as exc:
            report["missing"].append({"task": task["id"], "reason": str(exc)})
        else:
            report["updated"].append(task["id"])
    print(json.dumps(report))
