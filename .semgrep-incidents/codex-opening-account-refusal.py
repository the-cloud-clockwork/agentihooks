def missed_opening(connection: object, events: list[dict], deadline: float, clock: object) -> str:
    # ruleid: codex-opening-account-refusal
    turn = opening_end(connection, events, deadline, clock)
    error = str((turn.get("error") or {}).get("message"))
    if CAPACITY not in error:
        return "turn-opening"
    raise RuntimeError(error)


def rejected_opening(connection: object, events: list[dict], deadline: float, clock: object) -> str:
    # ok: codex-opening-account-refusal
    turn = opening_end(connection, events, deadline, clock)
    if reason := account_refusal(turn):
        raise RuntimeError(reason)
    error = str((turn.get("error") or {}).get("message"))
    if CAPACITY not in error:
        return "turn-opening"
    raise RuntimeError(error)
