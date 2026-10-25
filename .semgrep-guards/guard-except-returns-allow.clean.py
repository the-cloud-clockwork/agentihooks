from hooks.hook_manager import BlockAction


def check(payload):
    try:
        verdict = payload["verdict"]
    except KeyError as exc:
        raise BlockAction("guard could not read its verdict") from exc
    if verdict:
        raise BlockAction(verdict)


def check_early(payload):
    try:
        return payload["skip"]
    except KeyError:
        pass
    raise BlockAction("no skip")


def advisory(payload, log):
    try:
        return payload["note"]
    except KeyError:
        log("no note")
        return None
