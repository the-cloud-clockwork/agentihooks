from typing import Any


def _unmarked_masters(agents: list[Any], hive: str, counts: dict[str, Any], spellings: dict[str, str]) -> int:
    return 0


def marker_only(hive: dict[str, Any], reported: dict[str, int], masters: int, pending: int) -> None:
    # ruleid: hive-worker-total-counts-unmarked-masters
    hive["agents"] = max(0, sum(reported.values()) - masters) + pending


def unsubtracted(hive: dict[str, Any], reported: dict[str, int], pending: int) -> None:
    # ruleid: hive-worker-total-counts-unmarked-masters
    hive["agents"] = max(0, sum(reported.values())) + pending


def by_role(hive: dict[str, Any], reported: dict[str, int], masters: int, pending: int, agents: list[Any]) -> None:
    # ok: hive-worker-total-counts-unmarked-masters
    hive["agents"] = max(0, sum(reported.values()) - masters - _unmarked_masters(agents, hive["id"], {}, {})) + (
        pending
    )


def floor(hive: dict[str, Any], recorded: int) -> None:
    # ok: hive-worker-total-counts-unmarked-masters
    hive["agents"] = max(hive["agents"], recorded)
