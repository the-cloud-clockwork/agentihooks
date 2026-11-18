import sys

from hooks.context.branch_guard import check_branch_guard
from hooks.context.venv_guard import check_venv_guard
from hooks.hook_manager import BlockAction, log


# ruleid: shell-guards-skip-venv-guard
def _check_shell_guards_without_the_venv_guard(payload: dict[str, object]) -> None:
    if payload.get("tool_name") != "Bash":
        return
    try:
        check_branch_guard(payload)
    except BlockAction:
        raise
    except Exception as e:
        log("branch_guard check failed", {"error": str(e)})


# ruleid: shell-guards-skip-venv-guard
def _check_shell_guards_that_only_name_the_venv_guard(payload: dict[str, object]) -> None:
    print(check_venv_guard.__name__, payload, file=sys.stderr)


# ok: shell-guards-skip-venv-guard
def _check_shell_guards_with_the_venv_guard_in_its_own_try(payload: dict[str, object]) -> None:
    if payload.get("tool_name") != "Bash":
        return
    check_branch_guard(payload)
    try:
        check_venv_guard(payload)
    except BlockAction:
        raise
    except Exception as e:
        log("venv_guard check failed", {"error": str(e)})


# ok: shell-guards-skip-venv-guard
def _check_shell_guards_with_the_venv_guard(payload: dict[str, object]) -> None:
    check_venv_guard(payload)


# ok: shell-guards-skip-venv-guard
def check_other_guards(payload: dict[str, object]) -> None:
    check_branch_guard(payload)
