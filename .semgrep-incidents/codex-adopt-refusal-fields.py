from scripts.codex_session.supervisor import NOT_ADMITTED, LaunchFailure


def opaque(execution_id: str) -> None:
    # ruleid: codex-adopt-refusal-fields
    raise LaunchFailure("execution", NOT_ADMITTED.format(execution_id))


def explicit(execution_id: str, details: str) -> None:
    # ok: codex-adopt-refusal-fields
    raise LaunchFailure("execution", f"{NOT_ADMITTED.format(execution_id)}: {details}")
