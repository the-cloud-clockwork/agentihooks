"""Phase checkboxes kept by the minute tick: a phase with tasks is done exactly when every one of them is done.

A phase without tasks is left to the master. Each change carries a status comment and an item for the master.
"""

from scripts.swarm.ledger_events import SENDER, Mail

TICKED = "Every task in this phase is done, so the swarm ticked it."
REOPENED = "A task that is not done landed in this phase, so the swarm reopened it."


def phase_pass(inbox, store, slug, doc, ledger):
    tasks = [t for t in doc.get("tasks", []) if not t.get("out_of_scope")]
    mail, actions = Mail(inbox, store, slug), []
    for phase in doc.get("phases", []):
        mine = [t for t in tasks if t.get("phase") == phase["id"]]
        if phase.get("out_of_scope") or not mine:
            continue
        open_ids = [t["id"] for t in mine if t.get("state") != "done"]
        done = not open_ids
        if done == bool(phase.get("done")):
            continue
        ledger.set_phase(slug, phase["id"], done, TICKED if done else REOPENED)
        if done:
            text = f"For your information: phase {phase['id']} {phase['title']} is done, all {len(mine)} tasks closed."
        else:
            text = f"For your information: phase {phase['id']} {phase['title']} reopened for {', '.join(open_ids)}."
        inbox.send(SENDER, mail.master, text)
        actions.append(f"phase {phase['id']} {'ticked' if done else 'reopened'}")
    return actions
