# ruleid: feature-quick-swarm-pane-typing
def _busy(seat, herdr):
    status = herdr.status(seat.pane_id)
    if status not in READY_STATES:
        return f"skipped-{status}"
    return "skipped-typed" if herdr.typed(seat.pane_id) else ""


# ok: feature-quick-swarm-pane-typing
def _busy(seat, herdr):
    if herdr.bare(seat.pane_id):
        return "skipped-bare"
    status = herdr.status(seat.pane_id)
    if status not in READY_STATES:
        return f"skipped-{status}"
    return "skipped-typed" if herdr.typed(seat.pane_id) else ""


def tell_master(folder, seat, environ):
    # ruleid: feature-quick-swarm-pane-typing
    herdr_host.prompt(seat.pane_id, ROOM.format(count=1), environ)


def run_in_seat(seat, launcher, environ):
    # ruleid: feature-quick-swarm-pane-typing
    herdr_host.run(seat.pane_id, launcher, environ)


class LiveHerdr:
    def prompt(self, pane_id, text):
        # ok: feature-quick-swarm-pane-typing
        return herdr_host.prompt(pane_id, text, self.environ)
