from scripts.swarm import claim_hold, freeze, phase_state
from scripts.swarm_ledger import task_dependencies


def ready(task, rows, doc, graph, fix):
    return (
        task.get("state") == "open"
        # ruleid: claim-filter-bypasses-claim-hold
        and phase_state.admits(task, doc)
        # ruleid: claim-filter-bypasses-claim-hold
        and not freeze.held(task, doc, graph, fix)
        # ruleid: claim-filter-bypasses-claim-hold
        and task_dependencies.ready(task, rows)
    )


def claimable(tasks, doc, graph, fix):
    # ruleid: claim-filter-bypasses-claim-hold
    return [t for t in tasks if not freeze.held(t, doc, graph, fix)]


def ready_through_the_hold(task, rows, doc, graph, fix):
    # ok: claim-filter-bypasses-claim-hold
    return task.get("state") == "open" and not claim_hold.hold(task, rows, doc, graph, fix)


def guarded(doc, graph, fix):
    # ok: claim-filter-bypasses-claim-hold
    return [t for t in doc["tasks"] if not freeze.held(t, doc, graph, fix)]
