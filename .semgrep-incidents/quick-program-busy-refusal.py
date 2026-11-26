from scripts.quick import recipe


# ruleid: quick-program-busy-refusal
def _busy(seat: object, herdr: object) -> str:
    shell = herdr.bare(seat.pane_id)
    if shell is not False:
        return "skipped-bare"
    status = herdr.status(seat.pane_id)
    return "" if status == "idle" else f"skipped-{status}"


# ok: quick-program-busy-refusal
def _busy(seat: object, herdr: object) -> str:
    shell = herdr.bare(seat.pane_id)
    if shell is not False:
        return "skipped-bare"
    if recipe.kind(seat) == recipe.PROGRAM:
        return "skipped-program"
    status = herdr.status(seat.pane_id)
    return "" if status == "idle" else f"skipped-{status}"
