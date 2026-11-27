def local_epoch_only(report: dict[str, str], member: str, current: str) -> bool:
    # ruleid: hive-restart-check-ignores-departed-members
    if current and report.get("member") == member and (report.get("epoch") or current) != current:
        return True
    return False


def departed_members_checked(report: dict[str, str], member: str, current: str, members: dict[str, object]) -> bool:
    # ok: hive-restart-check-ignores-departed-members
    if (current and report.get("member") == member and (report.get("epoch") or current) != current) or (
        members and report.get("member") and report["member"] not in members
    ):
        return True
    return False
