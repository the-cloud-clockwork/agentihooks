"""The stored quota capacity decision as swarm status lines and as the ledger page reads it."""

from scripts import quota_freshness, quota_state
from scripts.routing.slots import API
from scripts.swarm import capacity
from scripts.swarm.health.findings import MINUTE_MS
from scripts.swarm.store import AUTO_SCALING


def routing_left(account: dict) -> float | None:
    return quota_state.routing_left(account["harness"], account["five_left"], account["week_left"])


def left_text(value: float | None) -> str:
    return "unknown" if value is None else f"{value:g}% left"


def state_text(account: dict) -> str:
    return account["state"].lower()


def _changed(at: int, now_ms: int) -> str:
    minutes = (now_ms - at) // MINUTE_MS
    if minutes < 1:
        return "changed just now"
    return f"changed {minutes} minute ago" if minutes == 1 else f"changed {minutes} minutes ago"


def api_share(account: dict, accounts: list[dict]) -> tuple[int, int]:
    """(percent, total): the api row's share of every live session on its harness."""
    total = sum(row["sessions"] for row in accounts if row["harness"] == account["harness"])
    return (round(100 * account["sessions"] / total) if total else 0), total


def _account_line(row: dict, accounts: list[dict], now_ms: int) -> str:
    if row.get("kind") != API:
        detail = f"routing {left_text(routing_left(row))}"
        detail += f", {row['reserved']}" if row.get("reserved") else ""
        detail += f", read {quota_freshness.age(row['observed_at'], now_ms / 1000)} ago" if "observed_at" in row else ""
    else:
        share, total = api_share(row, accounts)
        weight = "no weight" if row.get("weight") is None else f"weight {row['weight']}%"
        detail = f"api share {share}% of {total} sessions against {weight}"
    return f"quota account {row['harness']} {row['name']}  {state_text(row)}  {detail}  sessions {row['sessions']}"


def live(agents: list) -> dict[str, int]:
    return {lane: sum(a.lane == lane and a.state != "finished" for a in agents) for lane in capacity.LANES}


def scaling_line(decision: dict, counts: dict[str, int], scaling: str) -> str:
    scaled = decision.get("autoscale") if scaling == AUTO_SCALING else None
    ceilings = scaled["ceilings"] if scaled else decision["configured"]
    lanes = ", ".join(f"{lane} {counts[lane]} of {ceilings[lane]}" for lane in capacity.LANES)
    seats = decision.get("placeable")
    line = f"scaling {scaling}, live of ceiling {lanes}"
    if seats:
        line += f", free quota seats claude {seats['claude']} and codex {seats['codex']}"
    return line + (f", because {scaled['reason'].removesuffix('.')}" if scaled else "")


def lines(decision: dict, now_ms: int, counts: dict[str, int], scaling: str) -> list[str]:
    if not decision:
        return [capacity.status_line(decision)]
    caps = ", ".join(
        f"{lane} {decision['effective'][lane]} of {decision['configured'][lane]}" for lane in capacity.LANES
    )
    head = f"quota capacity {caps}, {_changed(decision['at'], now_ms)}, because {decision['reason']}"
    host = [host_line(decision["host"])] if "host" in decision else []
    accounts = [_account_line(row, decision["accounts"], now_ms) for row in decision["accounts"]]
    return [head, scaling_line(decision, counts, scaling)] + host + accounts


def host_line(host: dict) -> str:
    room = "unknown" if host["room"] is None else host["room"]
    return f"host room {room}: {host['reason']}"


def page(decision: dict) -> dict:
    if not decision:
        return decision
    rows = decision["accounts"]
    accounts = [
        {
            **row,
            "routing": routing_left(row),
            **({"share": api_share(row, rows)[0]} if row.get("kind") == API else {}),
        }
        for row in rows
    ]
    return {**decision, "lanes": list(capacity.LANES), "accounts": accounts}
