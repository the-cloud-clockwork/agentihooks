REASONS = ()
PULL_WAITING = frozenset()
RELEASES = ()
PULL_GRACE_MS = 0
NO_TAIL = ""


class Decision:
    pass


class AccountSlot:
    pass


class Recovery:
    pass


def classify(pod, ready=None):
    raise NotImplementedError


def tail(reason, session, watermark):
    raise NotImplementedError
