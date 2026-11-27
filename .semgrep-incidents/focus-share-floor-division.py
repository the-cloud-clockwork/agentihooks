def floor_focus_share(caps: dict[str, int], weight: int) -> dict[str, int]:
    # ruleid: focus-share-floor-division
    return {lane: (count * weight) // 100 for lane, count in caps.items()}


def optional_floor_focus_share(caps: dict[str, int] | None, weight: int) -> dict[str, int]:
    # ruleid: focus-share-floor-division
    return {lane: (count * weight) // 100 for lane, count in (caps or {}).items()}


def minimum_focus_share(caps: dict[str, int], weight: int) -> dict[str, int]:
    # ok: focus-share-floor-division
    return {lane: max(1, (count * weight) // 100) if count > 0 and weight > 0 else 0 for lane, count in caps.items()}


def optional_minimum_focus_share(caps: dict[str, int] | None, weight: int) -> dict[str, int]:
    # ok: focus-share-floor-division
    return {
        lane: max(1, (count * weight) // 100) if count > 0 and weight > 0 else 0 for lane, count in (caps or {}).items()
    }
