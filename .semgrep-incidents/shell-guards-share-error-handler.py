from hooks.context.branch_guard import check_branch_guard, check_commit_on_main
from hooks.context.venv_guard import check_venv_guard
from hooks.hook_manager import BlockAction, log


def _check_shell_guards_shared(payload: dict[str, object]) -> None:
    # ruleid: shell-guards-share-error-handler
    try:
        check_branch_guard(payload)
        check_commit_on_main(payload)
        check_venv_guard(payload)
    except BlockAction:
        raise
    except Exception as error:
        log("branch_guard check failed", {"error": str(error)})


def _check_shell_guards_reversed(payload: dict[str, object]) -> None:
    # ruleid: shell-guards-share-error-handler
    try:
        check_venv_guard(payload)
        check_branch_guard(payload)
    except Exception as error:
        log("venv_guard check failed", {"error": str(error)})


def _check_shell_guards_isolated(payload: dict[str, object]) -> None:
    # ok: shell-guards-share-error-handler
    try:
        check_branch_guard(payload)
    except BlockAction:
        raise
    except Exception as error:
        log("branch_guard check failed", {"error": str(error)})
    # ok: shell-guards-share-error-handler
    try:
        check_venv_guard(payload)
    except BlockAction:
        raise
    except Exception as error:
        log("venv_guard check failed", {"error": str(error)})


def check_other_guards(payload: dict[str, object]) -> None:
    # ok: shell-guards-share-error-handler
    try:
        check_branch_guard(payload)
        check_venv_guard(payload)
    except Exception as error:
        log("other guards check failed", {"error": str(error)})
