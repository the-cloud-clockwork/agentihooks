import sys

from scripts.events import state


class Refused(RuntimeError):
    pass


class LostLock(RuntimeError):
    pass


def run_due() -> None:
    return None


async def run_retries_a_refused_login(stopped: list[int]) -> bool:
    transient = state.unavailable()
    silence = state.unanswered()
    while not stopped:
        # ruleid: scheduler-credential-refusal-retried
        try:
            run_due()
        except LostLock as exc:
            print(f"scheduler pass ended: {exc}", file=sys.stderr)
        except silence:
            print("scheduler pass skipped: the state store did not answer", file=sys.stderr)
        except transient:
            print("scheduler pass skipped: the state store did not answer", file=sys.stderr)
    return False


async def run_logs_a_refused_login_and_retries(stopped: list[int]) -> bool:
    refused = state.refused()
    silence = state.unanswered()
    while not stopped:
        # ruleid: scheduler-credential-refusal-retried
        try:
            run_due()
        except refused as exc:
            print(f"scheduler pass skipped: {exc}", file=sys.stderr)
        except silence:
            print("scheduler pass skipped: the state store did not answer", file=sys.stderr)
    return False


async def run_stops_on_a_refused_login(stopped: list[int]) -> bool:
    transient = state.unavailable()
    refused = state.refused()
    silence = state.unanswered()
    while not stopped:
        # ok: scheduler-credential-refusal-retried
        try:
            run_due()
        except LostLock as exc:
            print(f"scheduler pass ended: {exc}", file=sys.stderr)
        except refused as exc:
            print(f"scheduler stops: the state store refused its credential ({exc})", file=sys.stderr)
            raise Refused(str(exc)) from exc
        except silence:
            print("scheduler pass skipped: the state store did not answer", file=sys.stderr)
        except transient:
            print("scheduler pass skipped: the state store did not answer", file=sys.stderr)
    return False
