from scripts.hive import account_slots


def coupled() -> int:
    # ruleid: hub-admission-answer-ms-from-slot-timeout
    return account_slots.REQUEST_TIMEOUT_MS - 2_000


def coupled_qualified() -> int:
    import scripts.hive.account_slots

    # ruleid: hub-admission-answer-ms-from-slot-timeout
    return scripts.hive.account_slots.REQUEST_TIMEOUT_MS - 2_000


def decoupled() -> int:
    # ok: hub-admission-answer-ms-from-slot-timeout
    return account_slots.ADMISSION_REQUEST_TIMEOUT_MS - 2_000
