from hooks.hook_manager import BlockAction


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


def check_bare(payload):
    # ruleid: guard-except-returns-allow
    try:
        verdict = payload["verdict"]
    except:  # noqa: E722
        return True
    if verdict:
        raise BlockAction(verdict)


def check_nested(payload, strict):
    # ruleid: guard-except-returns-allow
    try:
        verdict = payload["verdict"]
    except KeyError:
        if not strict:
            return "allow"
        verdict = "unreadable"
    if verdict:
        raise BlockAction(verdict)


async def check_async(payload):
    # ruleid: guard-except-returns-allow
    try:
        verdict = await payload.verdict()
    except OSError:
        return
    if verdict:
        raise BlockAction(verdict)
