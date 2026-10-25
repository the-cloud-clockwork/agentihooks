from hooks.common import BlockAction


def check(payload):
    try:
        verdict = payload["verdict"]
    except KeyError as exc:
        raise BlockAction("guard could not read its verdict") from exc
    if verdict:
        raise BlockAction(verdict)


def advisory(payload, log):
    try:
        return payload["note"]
    except KeyError:
        log("no note")
        return None
