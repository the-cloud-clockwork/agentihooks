from hooks.common import BlockAction


def check(payload):
    # ruleid: guard-except-returns-allow
    try:
        verdict = payload["verdict"]
    except KeyError:
        return
    if verdict:
        raise BlockAction(verdict)


def check_logged(payload, log):
    # ruleid: guard-except-returns-allow
    try:
        verdict = payload["verdict"]
    except Exception as exc:
        log("guard failed", {"error": str(exc)})
        return None
    if verdict:
        raise BlockAction(verdict)
