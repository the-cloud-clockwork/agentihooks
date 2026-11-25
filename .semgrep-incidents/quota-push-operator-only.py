from hooks.context.quota_policy import record_push_signal


def _prompt_quota_policy_untyped(payload: dict[str, str], session_id: str, typed: bool) -> bool:
    # ruleid: quota-push-operator-only
    return record_push_signal(session_id, payload.get("prompt", ""))


def _prompt_quota_policy_typed(payload: dict[str, str], session_id: str, typed: bool) -> bool:
    # ok: quota-push-operator-only
    if typed and record_push_signal(session_id, payload.get("prompt", "")):
        return True
    return False


def _prompt_quota_policy_nested(payload: dict[str, str], session_id: str, typed: bool) -> bool:
    if typed:
        # ok: quota-push-operator-only
        return record_push_signal(session_id, payload.get("prompt", ""))
    return False
