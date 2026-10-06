"""The Swarm tab layout: one set of panel sizes shared by every ledger page."""

import json

import ledger_core as core

ROWS = {
    "capacity-box": ("height",),
    "swarm-row-work": ("height", "split"),
    "swarm-row-accounts": ("height", "split"),
    "health-box": ("height",),
    "handoff-box": ("height",),
}
LIMITS = {"height": (48, 4000, 0), "split": (15, 85, 1)}


def path():
    return core.LEDGER_DIR / ".swarm-layout"


def size(row, key, value):
    low, high, digits = LIMITS[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f"{row} {key} must be a number from {low} to {high}")
    return round(value, digits) if digits else round(value)


def check(body):
    if not isinstance(body, dict):
        raise ValueError("layout must be an object of rows")
    layout = {}
    for row, sizes in body.items():
        if row not in ROWS:
            raise ValueError(f"unknown row {row}")
        if not isinstance(sizes, dict) or not sizes:
            raise ValueError(f"{row} must be an object of sizes")
        if set(sizes) - set(ROWS[row]):
            raise ValueError(f"{row} takes only {', '.join(ROWS[row])}")
        layout[row] = {key: size(row, key, value) for key, value in sizes.items()}
    return layout


def read():
    try:
        return check(core.loads(path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def loads(data):
    try:
        return core.loads(data or b"{}")
    except json.JSONDecodeError:
        raise ValueError("body is not JSON") from None


def write(body):
    layout = check(body)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    core.atomic_write(path(), json.dumps(layout))
    return layout
