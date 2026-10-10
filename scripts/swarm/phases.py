"""Phase checkboxes kept by the minute tick: a phase with tasks is done exactly when every one of them is done.

A phase without tasks is left to the master. Each change carries a status comment and an item for the master.
"""

from scripts.swarm.ledger_events import SENDER, Mail

TICKED = "Every task in this phase is done, so the swarm ticked it."
REOPENED = "A task that is not done landed in this phase, so the swarm reopened it."


def phase_pass(inbox, store, slug, doc, ledger):
    try:
        under = phase_tasks(ledger.hierarchy(slug))
    except OSError as exc:
        # A refused or unreachable hierarchy read (server not restarted, tree changed between pages) waits a tick.
        return [f"phase pass skipped, the hierarchy read failed: {exc}"]
    mail, actions = Mail(inbox, store, slug), []
    for phase in doc.get("phases", []):
        mine = [(tid, state) for tid, state in under.get(phase["id"], []) if state != "out_of_scope"]
        if phase.get("out_of_scope") or not mine:
            continue
        open_ids = [tid for tid, state in mine if state != "done"]
        done = not open_ids
        if done == bool(phase.get("done")):
            continue
        ledger.set_phase(slug, phase["id"], done, TICKED if done else REOPENED)
        if done:
            text = f"For your information: phase {phase['id']} {phase['title']} is done, all {len(mine)} tasks closed."
        else:
            text = f"For your information: phase {phase['id']} {phase['title']} reopened for {', '.join(open_ids)}."
        inbox.send(SENDER, mail.master, text, fyi=True)
        actions.append(f"phase {phase['id']} {'ticked' if done else 'reopened'}")
    return actions


def phase_tasks(rows: list) -> dict:
    found, phase = {}, None
    for row in rows:
        if phase is not None and row["depth"] <= phase[1]:
            phase = None
        if row["kind"] == "phase":
            phase = (row["node"].removeprefix("phases/"), row["depth"])
            found[phase[0]] = []
        elif row["kind"] == "task" and phase is not None:
            found[phase[0]].append((row["node"].removeprefix("tasks/"), row["state"]))
    return found
