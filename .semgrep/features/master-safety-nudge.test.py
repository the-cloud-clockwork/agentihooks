# ruleid: feature-master-safety-nudge
def nudge(herdr, entry):
    pane = Pane(entry["seat"], entry.get("harness") or "claude", entry["pane"])
    try:
        herdr.prompt(pane, TEXT)
    except Exception as error:
        return f"failed-{type(error).__name__}"
    return "sent"


# ruleid: feature-master-safety-nudge
def nudge(herdr, entry):
    pane = Pane(entry["seat"], entry.get("harness") or "claude", entry["pane"])
    try:
        status = herdr.agent_status(pane)
        if status not in READY:
            return f"skipped-{status}"
        herdr.prompt(pane, TEXT)
    except Exception as error:
        return f"failed-{type(error).__name__}"
    return "sent"


# ok: feature-master-safety-nudge
def nudge(herdr, entry):
    pane = Pane(entry["seat"], entry.get("harness") or "claude", entry["pane"])
    try:
        status = herdr.agent_status(pane)
        if status not in READY:
            return f"skipped-{status}"
        if herdr.typed_input(pane):
            return "skipped-typed"
        herdr.prompt(pane, TEXT)
    except Exception as error:
        return f"failed-{type(error).__name__}"
    return "sent"
