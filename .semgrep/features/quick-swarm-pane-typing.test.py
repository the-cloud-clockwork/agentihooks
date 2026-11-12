# ruleid: feature-quick-swarm-pane-typing
def _busy(seat, herdr):
    status = herdr.status(seat.pane_id)
    if status not in READY_STATES:
        return f"skipped-{status}"
    return "skipped-typed" if herdr.typed(seat.pane_id) else ""


# ruleid: feature-quick-swarm-pane-typing
def _busy(seat, herdr):
    if herdr.bare(seat.pane_id):
        return "skipped-bare"
    status = herdr.status(seat.pane_id)
    if status not in READY_STATES:
        return f"skipped-{status}"
    return "skipped-typed" if herdr.typed(seat.pane_id) else ""


# ok: feature-quick-swarm-pane-typing
def _busy(seat, herdr):
    shell = herdr.bare(seat.pane_id)
    if shell is not False:
        return "skipped-bare" if shell else "skipped-unknown"
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


def poke(folder, seat, herdr):
    # ruleid: feature-quick-swarm-pane-typing
    herdr.prompt(seat.pane_id, NUDGE)


def notify_without_checking(folder, seat, herdr):
    # ruleid: feature-quick-swarm-pane-typing
    return "room" if _room_notice(folder, seat, herdr) else "idle"


def tell_before_checking(folder, seat, herdr):
    # ruleid: feature-quick-swarm-pane-typing
    told = _room_notice(folder, seat, herdr)
    why = _busy(seat, herdr)
    if why:
        return why
    return "room" if told else "idle"


def _nudge(folder, seat, herdr, now):
    # ok: feature-quick-swarm-pane-typing
    return herdr.prompt(seat.pane_id, NUDGE)


def tell(folder, seat, herdr):
    why = _busy(seat, herdr)
    if why:
        return why
    # ok: feature-quick-swarm-pane-typing
    return "room" if _room_notice(folder, seat, herdr) else "idle"


class LiveHerdr:
    def prompt(self, pane_id, text):
        # ok: feature-quick-swarm-pane-typing
        return herdr_host.prompt(pane_id, text, self.environ)
