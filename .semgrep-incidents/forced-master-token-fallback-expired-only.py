from collections.abc import Callable

from scripts import master_claude_login as login
from scripts.routing import native_login


def expired_only(front: bool, take: Callable[[], None], args: list[str]) -> None:
    try:
        take()
    except login.LoginRefused as exc:
        # ruleid: forced-master-token-fallback-expired-only
        if front and str(exc) == native_login.EXPIRED:
            print(f"[agenti] claude ai login refused: {exc}")
            return login.launch_token("/opt/claude", args, {}, "")
        raise


def forced_or_expired(front: bool, forced: bool, take: Callable[[], None], args: list[str]) -> None:
    try:
        take()
    except login.LoginRefused as exc:
        # ok: forced-master-token-fallback-expired-only
        if forced or front and str(exc) == native_login.EXPIRED:
            print(f"[agenti] claude ai login refused: {exc}")
            return login.launch_token("/opt/claude", args, {}, "", forced=forced)
        raise
