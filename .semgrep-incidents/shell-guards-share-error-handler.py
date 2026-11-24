from hooks.context.branch_guard import check_branch_guard, check_commit_on_main
from hooks.context.master_coding import protection_refusal
from hooks.context.venv_guard import check_venv_guard
from hooks.hook_manager import BlockAction, _shell_guard, log


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


def _check_shell_guards_protection_after_branch(payload: dict[str, object]) -> None:
    # ruleid: shell-guards-share-error-handler
    try:
        check_branch_guard(payload)
        if refusal := protection_refusal(payload):
            raise BlockAction(refusal)
    except BlockAction:
        raise
    except Exception as error:
        log("branch_guard check failed", {"error": str(error)})


def _check_shell_guards_protection_before_venv(payload: dict[str, object]) -> None:
    # ruleid: shell-guards-share-error-handler
    try:
        if refusal := protection_refusal(payload):
            raise BlockAction(refusal)
        check_venv_guard(payload)
    except BlockAction:
        raise
    except Exception as error:
        log("master_coding check failed", {"error": str(error)})


def _check_shell_guards_protection_isolated(payload: dict[str, object]) -> None:
    # ok: shell-guards-share-error-handler
    try:
        if refusal := protection_refusal(payload):
            raise BlockAction(refusal)
    except BlockAction:
        raise
    except Exception as error:
        log("master_coding check failed", {"error": str(error)})
    # ok: shell-guards-share-error-handler
    try:
        check_venv_guard(payload)
    except BlockAction:
        raise
    except Exception as error:
        log("venv_guard check failed", {"error": str(error)})


def _check_shell_guards_grouped_with(payload: dict[str, object]) -> None:
    # ruleid: shell-guards-share-error-handler
    with _shell_guard("branch_guard"):
        check_branch_guard(payload)
        check_venv_guard(payload)


def _check_shell_guards_grouped_with_protection_after(payload: dict[str, object]) -> None:
    # ruleid: shell-guards-share-error-handler
    with _shell_guard("branch_guard"):
        check_commit_on_main(payload)
        if refusal := protection_refusal(payload):
            raise BlockAction(refusal)


def _check_shell_guards_grouped_with_protection_before(payload: dict[str, object]) -> None:
    # ruleid: shell-guards-share-error-handler
    with _shell_guard("master_coding"):
        if refusal := protection_refusal(payload):
            raise BlockAction(refusal)
        check_venv_guard(payload)


def _check_shell_guards_independent_with(payload: dict[str, object]) -> None:
    # ok: shell-guards-share-error-handler
    with _shell_guard("branch_guard"):
        check_branch_guard(payload)
    # ok: shell-guards-share-error-handler
    with _shell_guard("master_coding"):
        if refusal := protection_refusal(payload):
            raise BlockAction(refusal)
    # ok: shell-guards-share-error-handler
    with _shell_guard("venv_guard"):
        check_venv_guard(payload)
