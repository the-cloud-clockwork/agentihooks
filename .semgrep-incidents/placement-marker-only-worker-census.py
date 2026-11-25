def marker_only(counts: dict[str, int]) -> int:
    # ruleid: placement-marker-only-worker-census
    masters = sum(count for key, count in counts.items() if key.startswith("interactive:"))
    return masters


def role_aware(counts: dict[str, int], masters: set[str]) -> int:
    # ok: placement-marker-only-worker-census
    return max(counts.get("interactive:claude:account", 0), len(masters))
