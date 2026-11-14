from typing import Any


def skipped_guard(signer: Any, member_id: str) -> None:
    # ruleid: hub-mint-unscoped-member-grant
    if signer.member_swarms is not None and member_id not in signer.member_swarms:
        raise ValueError(member_id)


def skipped_lookup(signer: Any, redis: Any, value: str) -> str | None:
    # ruleid: hub-mint-unscoped-member-grant
    if signer.member_swarms is not None and (member := redis.get(value)):
        return member
    return None


def refused_record(signer: Any, member_id: str) -> None:
    # ok: hub-mint-unscoped-member-grant
    if signer.member_swarms is None:
        raise ValueError("no member_swarms field")
    if member_id not in signer.member_swarms:
        raise ValueError(member_id)
