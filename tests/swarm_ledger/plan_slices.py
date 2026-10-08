from scripts.swarm_ledger import ledger_artifacts, ledger_core


def anchored(slug, *names, phase="p1", core=ledger_core):
    text = f"# Plan for {phase}\n" + "".join(f"<!-- slice: {name} -->\nStep {name}\n" for name in names)
    file = ledger_artifacts.store(slug, "plan.md", text.encode())
    ref = {"artifact": f"http://127.0.0.1:8765/artifacts/{slug}/{file['id']}", "lines": f"1-{1 + 2 * len(names)}"}
    key = f"{phase}-{file['id'][:10]}"
    ops = [
        {"op": "join", "id": f"join-planner-{key}", "by": "planner", "role": "member"},
        {"op": "artifact_add", "id": f"art-{key}", "by": "planner", "task": "", "title": "Plan", "file": file},
        {"op": "phase_update", "id": f"ref-{key}", "by": "planner", "item": f"phases/{phase}"},
    ]
    ops[1]["plan"] = True
    ops[2]["fields"] = {"plan_ref": ref}
    for op in ops:
        core.check_op(op)
    state, rejected = core.sync(slug, ops=ops)
    assert rejected == []
    return state
