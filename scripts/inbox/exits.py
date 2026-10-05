"""Mail left pending for a swarm agent that exits: moved to its seat when the work goes on, else withdrawn and its
sender told."""

BY = "swarm"


def settle(inbox, name, seat, exit_text):
    """seat is where the work goes on, '' when nobody takes it up."""
    for item in inbox.pending_items(name):
        if seat:
            inbox.redirect(item.id, BY, seat, f"{name} {exit_text}; moved to {seat} for its next occupant")
        elif inbox.withdraw(item.id, BY, f"cancelled: {name} {exit_text} before reading it") and item.sender != BY:
            inbox.send(BY, item.sender, _told(item, name, exit_text))


def _told(item, name, exit_text):
    gist = item.text.splitlines()[0][:200] if item.text else ""
    return (
        f"{name} {exit_text} before reading your message {item.id}: {gist}. "
        "It is closed; send it to whoever carries that work on if it still matters."
    )
